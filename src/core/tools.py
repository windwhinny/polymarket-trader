"""Agent tools — search, market data, bet placement, enforced time constraints."""

import json
import logging
from typing import Optional
from datetime import datetime

log = logging.getLogger("pm-backtest.agent")


class AgentContext:
    """Mutable state for the agent during a month's trading session.

    Cash accounting:
      starting_capital — month-open equity (fixed; basis for risk limits)
      available_cash   — unallocated cash; decreases as bets are placed
      total_equity     — available_cash + sum(open bet stakes) + settled P&L;
                         this is what an external observer would call "capital"
    """

    def __init__(self, year: int, month: int, capital: float, markets: list, config: dict):
        self.year = year
        self.month = month
        self.month_key = f"{year}-{month:02d}"
        self.starting_capital = capital
        self.available_cash = capital
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
    def total_equity(self) -> float:
        """Cash + open stakes + settled P&L. Equals starting_capital + total realized P&L."""
        open_stakes = sum(b.amount for b in self.bets if b.pnl is None)
        realized = sum(b.pnl for b in self.bets if b.pnl is not None)
        return self.available_cash + open_stakes + realized

    # Back-compat: many call sites still read ctx.capital
    @property
    def capital(self) -> float:
        return self.total_equity

    @property
    def available_slugs(self):
        return list(self.markets.keys())

    def market_summary(self):
        lines = []
        for slug, m in self.markets.items():
            yes_price = m.outcome_prices[0] if m.outcome_prices else "?"
            end = (m.end_date or "")[:10]
            lines.append(f"  [{slug}] {m.question} | YES={yes_price} | vol=${m.volume:,.0f} | end={end}")
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
                "description": "在预测市场上下注。每次只下一个注。amount 不能超过 starting_capital 的 15%。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "市场 slug"},
                        "direction": {"type": "string", "enum": ["YES", "NO"]},
                        "amount": {"type": "number", "description": "下注金额（美元），不超过起始资金 15%"},
                        "model_prob": {
                            "type": "number",
                            "description": "你估计的 YES 真实概率（0-1）。和市场 YES 价格的差就是你认为的 edge。"
                        },
                        "reasoning": {"type": "string", "description": "下注理由（1-2句话）"}
                    },
                    "required": ["slug", "direction", "amount", "model_prob", "reasoning"]
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
    log.info("TOOL | %s | args=%s", name, json.dumps(args, ensure_ascii=False))

    if name == "search_news":
        return _search_news(args["query"], ctx)

    elif name == "get_market_detail":
        return _get_market_detail(args["slug"], ctx)

    elif name == "place_bet":
        return _place_bet(
            args["slug"], args["direction"], args["amount"],
            args.get("model_prob"),
            args.get("reasoning", ""), ctx,
        )

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
                "snippet": r.get("snippet", ""),
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


def _place_bet(slug: str, direction: str, amount: float,
               model_prob: Optional[float], reasoning: str, ctx: AgentContext) -> str:
    market = ctx.markets.get(slug)
    if not market:
        return json.dumps({"error": f"未找到市场: {slug}"}, ensure_ascii=False)

    if amount <= 0:
        return json.dumps({"error": "下注金额必须大于 0"}, ensure_ascii=False)

    # Risk limit is anchored to month-open equity, not the dwindling cash balance,
    # so the agent can place its planned 5-laid spread without the cap shrinking each turn.
    max_per_bet = ctx.starting_capital * 0.15
    if amount > max_per_bet:
        return json.dumps({
            "error": f"单笔下注不能超过起始资金的 15% (${max_per_bet:.0f})。请降低金额，分散到更多市场。",
            "starting_capital": ctx.starting_capital,
            "available_cash": round(ctx.available_cash, 2),
        }, ensure_ascii=False)

    if amount > ctx.available_cash:
        return json.dumps({
            "error": f"现金不足！可用现金 ${ctx.available_cash:.2f}，但想下注 ${amount:.2f}",
            "available_cash": round(ctx.available_cash, 2),
            "starting_capital": ctx.starting_capital,
        }, ensure_ascii=False)

    from .simulator import simulate_bet

    market_prob = market.outcome_prices[0] if market.outcome_prices else 0.5

    # model_prob is now supplied by the agent (probability of YES). Fall back to
    # the market price (zero edge) if missing or out of range.
    if model_prob is None or not (0.0 < model_prob < 1.0):
        model_prob = market_prob
    model_prob = max(0.01, min(0.99, float(model_prob)))

    # Edge in the direction of the bet: positive when the agent thinks the bet is +EV.
    if direction == "YES":
        edge = model_prob - market_prob
    else:
        edge = (1 - model_prob) - (1 - market_prob)  # = market_prob - model_prob

    bet = simulate_bet(
        market=market, month=ctx.month_key, direction=direction,
        model_prob=model_prob, market_prob=market_prob,
        edge=edge,
        kelly_fraction=amount / ctx.starting_capital,
        capital=ctx.starting_capital,
    )

    ctx.available_cash -= amount
    bet.resolution = None
    bet.pnl = None
    ctx.bets.append(bet)

    ctx.trade_log.append({
        "slug": slug, "direction": direction, "amount": amount,
        "model_prob": model_prob, "edge": edge,
        "reasoning": reasoning, "entry_price": bet.entry_price,
    })

    return json.dumps({
        "status": "ok",
        "bet_placed": f"{direction} ${amount:.2f} on [{slug}]",
        "entry_price": round(bet.entry_price, 4),
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "edge": round(edge, 4),
        "available_cash": round(ctx.available_cash, 2),
        "total_equity": round(ctx.total_equity, 2),
        "note": "下注已记录，结果将在月末结算后揭晓。",
        "reasoning_recorded": reasoning,
    }, ensure_ascii=False)


def _get_portfolio(ctx: AgentContext) -> str:
    bets_info = []
    for b in ctx.bets:
        bets_info.append({
            "slug": b.market_id[:20],
            "direction": b.direction,
            "amount": b.amount,
            "model_prob": b.model_prob,
            "pnl": b.pnl,
            "resolution": b.resolution,
        })

    return json.dumps({
        "starting_capital": round(ctx.starting_capital, 2),
        "available_cash": round(ctx.available_cash, 2),
        "total_equity": round(ctx.total_equity, 2),
        "realized_pnl": round(ctx.total_equity - ctx.starting_capital, 2),
        "open_bets": len([b for b in ctx.bets if b.pnl is None]),
        "settled_bets": len([b for b in ctx.bets if b.pnl is not None]),
        "bets": bets_info,
    }, ensure_ascii=False)


def _finish_trading(summary: str, decisions: str, ctx: AgentContext) -> str:
    ctx.trade_log.append({"action": "finish", "summary": summary, "decisions": decisions})
    equity = ctx.total_equity
    return json.dumps({
        "status": "done",
        "month": ctx.month_key,
        "starting_capital": round(ctx.starting_capital, 2),
        "available_cash": round(ctx.available_cash, 2),
        "total_equity_pre_settle": round(equity, 2),
        "total_return_pre_settle": f"{(equity - ctx.starting_capital) / ctx.starting_capital * 100:.1f}%",
        "total_bets": len(ctx.bets),
        "summary": summary,
    }, ensure_ascii=False)
