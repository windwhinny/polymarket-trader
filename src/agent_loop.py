"""Agent Loop — autonomous trading agent with tool calling."""

import json
import logging
from typing import Optional

from .agent_tools import AgentContext, build_tools, execute_tool

log = logging.getLogger("pm-backtest.loop")

SYSTEM_PROMPT_TEMPLATE = """你是一个自主预测市场交易员，当前时间是 {month} 年 {month_num} 月底。
你有 ${capital:,.2f} 资金可以用于投资。

【业绩目标】
目标年化收益率 10-20%，即每月约 1-1.5%。不要过度冒险追求高回报。

【时间约束】
你只能看到 {cutoff_date} 之前的公开信息。所有搜索结果都已限制在该日期之前。
市场数据反映了 {cutoff_date} 时的真实价格。

【可用市场（含当前价格）】
{markets}

【交易流程】
1. 快速浏览可用市场列表，标记你认为定价有偏差的几个
2. 对感兴趣的市场调用 get_market_detail
3. 可选：调用 search_news 补充信息
4. 调用 place_bet 下注（每次一单）
5. 下注 2-4 个后，调用 finish_trading 结束
6. 不要反复搜索——信息够就下注，不够就跳过

【你的优势】
- 常识和基础知识：伦敦二月很冷、短期比特币涨跌接近随机、低概率事件市场常高估等
- 从价格中发现异常：比如"伦敦二月温度超过 18°C"价格 46%——明显太高
- 搜索无结果很正常，用你的判断力直接评估

【风险管理】
- 每次下注 5-10% 资金（最多 $100/笔），分散风险
- 至少下注 3-5 个不同市场，避免过度集中
- 不确定时跳过（不下注也是合理的策略）
- 目标每月赚 1-1.5%，不求暴利，稳健第一
- 优先保证不亏钱，而不是追求高收益
- 以中文交流"""


def run_agent_month(ctx: AgentContext, api_key: str, base_url: str, model: str = "deepseek-chat") -> AgentContext:
    """Run the autonomous agent for one month. Returns updated context with bets."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    tools = build_tools()

    system_msg = SYSTEM_PROMPT_TEMPLATE.format(
        month=f"{ctx.year}年{ctx.month}月",
        month_num=ctx.month,
        capital=ctx.capital,
        cutoff_date=ctx.cutoff_date,
        markets=ctx.market_summary(),
    )

    messages = [{"role": "system", "content": system_msg}]

    log.info("AGENT START | %s | capital=%.2f | %d markets",
             ctx.month_key, ctx.capital, len(ctx.markets))

    max_turns = 30
    turn = 0

    while turn < max_turns:
        turn += 1
        log.debug("AGENT TURN %d", turn)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=1000,
            )
        except Exception as e:
            log.error("API ERROR turn %d: %s", turn, e)
            break

        msg = response.choices[0].message

        # Check if model wants to call a function
        if msg.tool_calls:
            for tc in msg.tool_calls:
                func_name = tc.function.name
                try:
                    func_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    func_args = {}

                result = execute_tool(func_name, func_args, ctx)

                # Add assistant's function call + tool result to messages
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": func_name,
                            "arguments": tc.function.arguments,
                        }
                    }]
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

                # Check if finish_trading was called
                if func_name == "finish_trading":
                    log.info("AGENT FINISHED | %s | %d bets | capital %.2f → %.2f",
                             ctx.month_key, len(ctx.bets), ctx.starting_capital, ctx.capital)
                    return ctx

        elif msg.content:
            # Model thinking out loud
            messages.append({"role": "assistant", "content": msg.content})
            log.debug("AGENT THINK | %s", msg.content[:150])

        else:
            log.warning("AGENT | empty response at turn %d", turn)
            break

    log.warning("AGENT MAX TURNS | %s | stopped after %d turns", ctx.month_key, turn)
    return ctx
