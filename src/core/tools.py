"""Agent tools — search, market data, bet placement, enforced time constraints."""

import json
import logging
from typing import Optional
from datetime import datetime

log = logging.getLogger("pm-backtest.agent")


class AgentContext:
    """Mutable state for the agent during a month's trading session."""

    def __init__(self, year: int, month: int, capital: float, markets: list, config: dict):
        self.year = year
        self.month = month
        self.month_key = f"{year}-{month:02d}"
        self.capital = capital
        self.starting_capital = capital
        self.markets = {m.slug: m for m in markets}
        self.bets = []
        self.trade_log = []
        self.config = config

        # Date boundary
        from datetime import timedelta, timezone
        if month == 12:
            self.cutoff = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        else:
            self.cutoff = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        self.cutoff_date = self.cutoff.strftime("%Y-%m-%d")

    @property
    def available_slugs(self):
        return list(self.markets.keys())

    def market_summary(self):
        lines = []
        for slug, m in self.markets.items():
            yes_price = m.outcome_prices[0] if m.outcome_prices else "?"
            lines.append(f"  [{slug}] {m.question[:80]} | YES={yes_price} | vol=${m.volume:,.0f}")
        return "\n".join(lines)


def build_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_news",
                "description": "搜索某个话题的近期新闻和信息。搜索范围自动限制在当前月份结束之前。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，用中文或英文均可"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_market_detail",
                "description": "获取某个预测市场的详细信息，包括当前 YES/NO 价格、成交量、结算规则等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "市场标识符(slug)"}
                    },
                    "required": ["slug"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "place_bet",
                "description": "在预测市场上下注。请基于你的分析做出决策，说明下注理由。每次只下一个注。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "市场 slug"},
                        "direction": {"type": "string", "enum": ["YES", "NO"]},
                        "amount": {"type": "number", "description": "下注金额（美元），建议不超过总资金的 25%"},
                        "reasoning": {"type": "string", "description": "下注理由（1-2句话）"}
                    },
                    "required": ["slug", "direction", "amount", "reasoning"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_portfolio",
                "description": "查看当前投资组合状态，包括剩余资金和已下注列表。",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "finish_trading",
                "description": "本月的交易决策已完成，提交最终结果。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "strategy_summary": {"type": "string", "description": "本月策略总结"},
                        "key_decisions": {"type": "string", "description": "关键决策及其理由"}
                    },
                    "required": ["strategy_summary", "key_decisions"]
                }
            }
        }
    ]


def execute_tool(name: str, args: dict, ctx: AgentContext) -> str:
    """Execute a tool call and return the result as a JSON string."""
    log.info("TOOL | %s | args=%s", name, json.dumps(args, ensure_ascii=False)[:200])

    if name == "search_news":
        return _search_news(args["query"], ctx)

    elif name == "get_market_detail":
        return _get_market_detail(args["slug"], ctx)

    elif name == "place_bet":
        return _place_bet(args["slug"], args["direction"], args["amount"], args.get("reasoning", ""), ctx)

    elif name == "get_portfolio":
        return _get_portfolio(ctx)

    elif name == "finish_trading":
        return _finish_trading(args["strategy_summary"], args["key_decisions"], ctx)

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


def _search_news(query: str, ctx: AgentContext) -> str:
    from .search import search_context
    from .config import Cache

    cache = Cache(ctx.config["cache"]["dir"], ctx.config["cache"]["ttl_hours"])

    result = search_context(
        query=query,
        end_date=ctx.cutoff_date,
        serpapi_api_key=ctx.config["api_keys"]["serpapi"]["key"],
        cache=cache,
        max_results=5,
    )

    if not result.results:
        return json.dumps({
            "status": "no_results",
            "query": query,
            "note": "搜索无结果。请根据你的知识做判断，或换关键词尝试。",
        }, ensure_ascii=False)

    response = {
        "status": "ok",
        "query": query,
        "result_count": len(result.results),
        "articles": []
    }
    for r in result.results:
        response["articles"].append({
            "title": r.get("title", ""),
            "snippet": r.get("snippet", "")[:300],
            "date": r.get("date", ""),
            "source": r.get("link", ""),
        })

    return json.dumps(response, ensure_ascii=False)


def _get_market_detail(slug: str, ctx: AgentContext) -> str:
    market = ctx.markets.get(slug)
    if not market:
        return json.dumps({"error": f"未找到市场: {slug}", "available": ctx.available_slugs}, ensure_ascii=False)

    return json.dumps({
        "slug": market.slug,
        "question": market.question,
        "category": market.category or "General",
        "yes_price": market.outcome_prices[0] if market.outcome_prices else "N/A",
        "no_price": market.outcome_prices[1] if len(market.outcome_prices) > 1 else "N/A",
        "volume_usd": f"${market.volume:,.0f}",
        "resolution_date": market.end_date,
        "outcomes": market.outcomes,
    }, ensure_ascii=False)


def _place_bet(slug: str, direction: str, amount: float, reasoning: str, ctx: AgentContext) -> str:
    market = ctx.markets.get(slug)
    if not market:
        return json.dumps({"error": f"未找到市场: {slug}"}, ensure_ascii=False)

    if amount <= 0:
        return json.dumps({"error": "下注金额必须大于 0"}, ensure_ascii=False)

    if amount > ctx.capital * 0.15:
        return json.dumps({
            "error": f"单笔下注不能超过总资金的 15% (${ctx.capital * 0.15:.0f})。请降低金额，分散到更多市场。",
            "your_capital": ctx.capital
        }, ensure_ascii=False)

    if amount > ctx.capital:
        return json.dumps({
            "error": f"资金不足！你只有 ${ctx.capital:.2f}，但想下注 ${amount:.2f}",
            "your_capital": ctx.capital
        }, ensure_ascii=False)

    from .simulator import simulate_bet, SPREAD_COST_RATE

    market_prob = market.outcome_prices[0] if market.outcome_prices else 0.5
    model_prob = market_prob + (0.08 if direction == "YES" else -0.08)
    model_prob = max(0.01, min(0.99, model_prob))
    edge = abs(model_prob - market_prob)

    bet = simulate_bet(
        market=market, month=ctx.month_key, direction=direction,
        model_prob=model_prob, market_prob=market_prob,
        edge=edge, kelly_fraction=amount / ctx.capital, capital=ctx.capital,
    )

    # Reserve capital — do NOT settle now (avoid look-ahead bias)
    ctx.capital -= amount
    bet.resolution = None
    bet.pnl = None
    ctx.bets.append(bet)

    ctx.trade_log.append({
        "slug": slug, "direction": direction, "amount": amount,
        "reasoning": reasoning, "entry_price": bet.entry_price,
    })

    return json.dumps({
        "status": "ok",
        "bet_placed": f"{direction} ${amount:.2f} on [{slug}]",
        "entry_price": round(bet.entry_price, 4),
        "note": "下注已记录，结果将在月末结算后揭晓。",
        "remaining_capital": round(ctx.capital, 2),
        "reasoning_recorded": reasoning,
    }, ensure_ascii=False)


def _get_portfolio(ctx: AgentContext) -> str:
    bets_info = []
    for b in ctx.bets:
        bets_info.append({
            "slug": b.market_id[:20],
            "direction": b.direction,
            "amount": b.amount,
            "pnl": b.pnl,
            "resolution": b.resolution,
        })

    return json.dumps({
        "starting_capital": ctx.starting_capital,
        "current_capital": round(ctx.capital, 2),
        "total_pnl": round(ctx.capital - ctx.starting_capital, 2),
        "open_bets": len([b for b in ctx.bets if b.pnl is None]),
        "settled_bets": len([b for b in ctx.bets if b.pnl is not None]),
        "bets": bets_info,
    }, ensure_ascii=False)


def _finish_trading(summary: str, decisions: str, ctx: AgentContext) -> str:
    ctx.trade_log.append({"action": "finish", "summary": summary, "decisions": decisions})
    return json.dumps({
        "status": "done",
        "month": ctx.month_key,
        "final_capital": round(ctx.capital, 2),
        "total_return": f"{(ctx.capital - ctx.starting_capital) / ctx.starting_capital * 100:.1f}%",
        "total_bets": len(ctx.bets),
        "summary": summary,
    }, ensure_ascii=False)
