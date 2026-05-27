"""Real-time prediction — analyze active markets, output bet recommendations."""

import json
import logging
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core.llm import LLMClient, LLMConfig
from src.core.tools import build_tools, execute_tool, AgentContext
from src.core.config import load_config, Cache
from src.core.tracer import create_run_id
from src.core.search import search_context

log = logging.getLogger("pm-trader.predict")

PREDICT_PROMPT = """你是一个预测市场分析师。当前时间: {now}。

分析下面这个市场，判断它是否被错误定价，并给出下注建议。

【市场】
问题: {question}
类别: {category}
YES 价格: {yes_price}（市场认为概率 {yes_pct}）
NO 价格: {no_price}
成交量: {volume}
结算日期: {end_date}

【你的任务】
1. 可以调用 search_news 搜索相关信息（可选）
2. 调用 get_market_detail 了解市场细节（如果需要）
3. 最终调用 place_prediction 给出你的判断

【place_prediction 参数】
- direction: "YES" | "NO" | "SKIP"
- amount: 建议下注金额 (总资金 ${capital}, 单笔 5-15%)
- confidence: "high" | "medium" | "low"
- reasoning: 1-2 句话说明理由
- edge_direction: 市场是"高估了 YES" 还是 "低估了 YES" 还是 "定价合理"

如果市场定价合理（无 edge），选 SKIP。
请在 5 轮内完成分析。"""


def _screen_markets(markets: list[dict], config: dict, llm_cfg: LLMConfig, capital: float) -> list[str]:
    """Let the agent pick which markets to analyze from the full list."""
    if len(markets) <= 10:
        return [m["slug"] for m in markets]

    client = LLMClient(llm_cfg)

    # Build compact market list
    market_lines = []
    for i, m in enumerate(markets):
        q = m["question"][:80]
        market_lines.append(
            f"{i+1}. [{m['slug']}] YES={m['yes_price']:.1%} | vol=${m['volume']:,.0f} | {q}"
        )
    market_list = "\n".join(market_lines)

    prompt = f"""你是一个预测市场筛选员。下面是 {len(markets)} 个活跃市场，请快速浏览。

{market_list}

选出你认为存在明显定价偏差的 5-10 个市场。判断标准：
- YES 价格与你对事件概率的直觉明显不符
- 你有相关领域知识可以评估
- 跳过纯 50/50 随机市场

直接输出 JSON 数组，仅包含 market slug：
{{"selected": ["slug1", "slug2", ...], "reasoning": "一句话说明筛选逻辑"}}"""

    messages = [{"role": "user", "content": prompt}]
    try:
        content, _ = client.chat(messages, [], temperature=0.2, max_tokens=500)
        result = json.loads(content) if isinstance(content, str) else content
        selected = result.get("selected", [])
        log.info("Screen: %d → %d selected | %s", len(markets), len(selected),
                 result.get("reasoning", "")[:80])
        return selected[:15]
    except Exception as e:
        log.error("Screen error: %s, analyzing all", e)
        return [m["slug"] for m in markets[:10]](min_volume: float = 5000, limit: int = 20, cache: Cache = None) -> list[dict]:
    """Fetch currently active markets from Gamma API."""
    import requests
    import json as _json

    cache_key = ("active-markets", str(min_volume), str(limit))
    if cache:
        cached = cache.get(*cache_key)
        if cached:
            return cached

    all_markets = []
    for offset in [0, 50]:
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": limit,
                        "offset": offset, "order": "volume", "ascending": "false"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error("Gamma API error: %s", e)
            break

        for raw in data:
            outcomes_raw = raw.get("outcomes", "[]")
            if isinstance(outcomes_raw, str):
                outcomes = _json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw
            if len(outcomes) != 2:
                continue

            prices_raw = raw.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                prices = _json.loads(prices_raw)
            else:
                prices = prices_raw
            if not prices or len(prices) < 2:
                continue

            vol = float(raw.get("volume", 0))
            if vol < min_volume:
                continue

            all_markets.append({
                "slug": raw.get("slug", ""),
                "question": raw.get("question", raw.get("title", "")),
                "outcomes": outcomes,
                "yes_price": float(prices[0]),
                "no_price": float(prices[1]),
                "volume": vol,
                "end_date": raw.get("endDate", ""),
                "category": raw.get("category", ""),
                "condition_id": raw.get("conditionId", ""),
            })

        if len(data) < limit:
            break

    if cache:
        cache.set(all_markets, *cache_key)

    return all_markets


def _analyze_market(market: dict, config: dict, llm_cfg: LLMConfig, capital: float) -> dict:
    """Run prediction agent for a single market. Returns recommendation dict."""
    client = LLMClient(llm_cfg)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    question = market["question"][:200]

    system_msg = PREDICT_PROMPT.format(
        now=now,
        question=question,
        category=market.get("category", "General"),
        yes_price=f"{market['yes_price']:.4f}",
        yes_pct=f"{market['yes_price']:.1%}",
        no_price=f"{market['no_price']:.4f}",
        volume=f"${market['volume']:,.0f}",
        end_date=market.get("end_date", "?"),
        capital=capital,
    )

    messages = [{"role": "system", "content": system_msg}]

    # Define prediction-specific tools
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_news",
                "description": "搜索与这个市场相关的最新新闻和信息。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "place_prediction",
                "description": "提交你的预测和下注建议。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["YES", "NO", "SKIP"]},
                        "amount": {"type": "number", "description": "建议下注金额 (USD)"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "reasoning": {"type": "string", "description": "1-2 句话理由"},
                        "edge_direction": {"type": "string", "description": '市场"高估了 YES"/"低估了 YES"/"定价合理"'}
                    },
                    "required": ["direction", "amount", "confidence", "reasoning", "edge_direction"]
                }
            }
        }
    ]

    result = {
        "slug": market["slug"],
        "question": question,
        "yes_price": market["yes_price"],
        "no_price": market["no_price"],
        "volume": market["volume"],
        "end_date": market.get("end_date", ""),
        "direction": "SKIP",
        "amount": 0,
        "confidence": "low",
        "reasoning": "",
        "edge_direction": "",
        "search_queries": [],
    }

    for turn in range(1, 8):
        try:
            content, tool_calls = client.chat(messages, tools, temperature=0.3, max_tokens=500)
        except Exception as e:
            log.error("[%s] API error turn %d: %s", market["slug"][:30], turn, e)
            result["reasoning"] = f"API error: {e}"
            break

        if tool_calls:
            for tc in tool_calls:
                func_name = tc["name"]
                func_args = tc.get("parsed_args", {})
                if not func_args:
                    try:
                        func_args = json.loads(tc.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        func_args = {}

                if func_name == "search_news":
                    query = func_args.get("query", "")
                    result["search_queries"].append(query)
                    ctx_result = search_context(
                        query=query, end_date=datetime.now().strftime("%Y-%m-%d"),
                        serpapi_api_key=config["api_keys"]["serpapi"]["key"],
                        max_results=3,
                    )
                    tool_result = ctx_result.summary[:2000] if ctx_result.summary else "(无结果)"
                    log.debug("[%s] search '%s': %d chars",
                              market["slug"][:20], query[:40], len(tool_result))

                elif func_name == "place_prediction":
                    result["direction"] = func_args.get("direction", "SKIP")
                    result["amount"] = func_args.get("amount", 0)
                    result["confidence"] = func_args.get("confidence", "low")
                    result["reasoning"] = func_args.get("reasoning", "")
                    result["edge_direction"] = func_args.get("edge_direction", "")
                    tool_result = json.dumps({"status": "recorded", "message": "预测已记录"}, ensure_ascii=False)
                else:
                    tool_result = json.dumps({"error": f"Unknown tool: {func_name}"})

                messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": func_name, "arguments": tc.get("arguments", "{}")}
                    }]
                })
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"], "content": tool_result,
                })

                if func_name == "place_prediction":
                    log.info("[%s] %s $%.0f (%.0f%% conf=%s) | %s",
                             market["slug"][:30], result["direction"],
                             result["amount"],
                             result["amount"] / capital * 100 if capital > 0 else 0,
                             result["confidence"], result["reasoning"][:60])
                    return result
        elif content:
            messages.append({"role": "assistant", "content": content})

    return result


def run_predict(config: dict, llm_cfg: LLMConfig, output_dir: str,
                capital: float = 1000, min_volume: float = 10000,
                parallel: int = 4) -> dict:
    """Run real-time predictions — agent picks markets, then analyzes in parallel."""
    cache_dir = Path(config["cache"]["dir"])
    if not cache_dir.is_absolute():
        cache_dir = Path(__file__).parent.parent.parent / cache_dir
    cache = Cache(str(cache_dir), 1)

    log.info("Fetching active markets (vol >= $%.0f)...", min_volume)
    all_markets = _fetch_active_markets(min_volume=min_volume, limit=50, cache=cache)
    log.info("Got %d markets total", len(all_markets))

    # Phase 1: Agent screens and picks interesting markets
    selected_slugs = _screen_markets(all_markets, config, llm_cfg, capital)
    markets = [m for m in all_markets if m["slug"] in selected_slugs]
    log.info("Phase 1 done: %d/%d markets selected for analysis", len(markets), len(all_markets))

    # Phase 2: Parallel analysis
    log.info("Phase 2: parallel analysis (workers=%d)...", parallel)
    results = []
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(_analyze_market, m, config, llm_cfg, capital): m["slug"]
            for m in markets
        }
        for f in as_completed(futures):
            slug = futures[f]
            try:
                result = f.result()
                results.append(result)
            except Exception as e:
                log.error("[%s] failed: %s", slug[:30], e)
                results.append({"slug": slug, "direction": "SKIP", "reasoning": f"Error: {e}"})

    # Sort: bets first, then skips
    results.sort(key=lambda r: (r["direction"] == "SKIP", -r.get("amount", 0)))

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _save_report(results, capital, out, llm_cfg)
    return {"markets_analyzed": len(results), "output_dir": str(out)}


def _save_report(results: list, capital: float, out_dir: Path, llm_cfg: LLMConfig):
    """Generate markdown report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    bets = [r for r in results if r["direction"] != "SKIP"]
    skips = [r for r in results if r["direction"] == "SKIP"]
    total_bet = sum(r["amount"] for r in bets)

    lines = [
        f"# Polymarket 实时预测报告",
        f"",
        f"**生成时间**: {now}",
        f"**模型**: {llm_cfg.provider}/{llm_cfg.model}",
        f"**总资金**: ${capital:,.0f}",
        f"**分析市场**: {len(results)} 个",
        f"**建议下注**: {len(bets)} 笔 | **跳过**: {len(skips)} 个",
        f"**总下注金额**: ${total_bet:,.0f} (仓位 {total_bet/capital*100:.0f}%)",
        f"",
        f"---",
        f"",
        f"## 下注建议 (按金额排序)",
        f"",
    ]

    if bets:
        lines.append(f"| # | 方向 | 金额 | 置信度 | YES价格 | 市场 | 理由 |")
        lines.append(f"|---|------|------|--------|--------|------|------|")
        for i, b in enumerate(bets, 1):
            slug_short = b["slug"][:45]
            lines.append(
                f"| {i} | **{b['direction']}** | ${b['amount']:.0f} | {b['confidence']} "
                f"| {b['yes_price']:.1%} | {slug_short} | {b['reasoning'][:80]} |"
            )
    else:
        lines.append("*(无下注建议)*")

    lines += [
        f"",
        f"## 跳过的市场",
        f"",
    ]
    if skips:
        for b in skips:
            lines.append(f"- [{b['slug'][:60]}]({b['yes_price']:.1%}) — {b.get('reasoning','')[:80]}")
    else:
        lines.append("*(全部市场均有下注建议)*")

    lines += [
        f"",
        f"---",
        f"",
        f"## 风险提示",
        f"- 此为 AI 模型分析结果，不构成投资建议",
        f"- 预测市场存在本金全部损失的风险",
        f"- 月末根据实际结算结果评估胜率",
    ]

    md_content = "\n".join(lines)
    md_path = out_dir / "recommendations.md"
    with open(md_path, "w") as f:
        f.write(md_content)

    # Also save JSON
    json_path = out_dir / "predictions.json"
    with open(json_path, "w") as f:
        json.dump({
            "generated_at": now,
            "model": f"{llm_cfg.provider}/{llm_cfg.model}",
            "capital": capital,
            "total_markets": len(results),
            "bets": bets,
            "skips": [{"slug": s["slug"], "reasoning": s.get("reasoning", "")} for s in skips],
        }, f, ensure_ascii=False, indent=2)

    log.info("Report saved: %s (%d bets, %d skips)", md_path, len(bets), len(skips))
