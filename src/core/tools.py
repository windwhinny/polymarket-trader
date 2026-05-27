"""Agent tools — search, market data, bet placement, enforced time constraints."""

import json
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone

log = logging.getLogger("pm-backtest.agent")


class AgentContext:
    """Mutable state for the agent during a single decision session.

    A "decision session" used to be a calendar month, but is now any point in
    time at which the agent surveys the live market list and places bets. The
    cutoff is `decision_dt` itself: searches and prices are pinned to that
    moment, and bets carry placed_at = decision_dt for later event-driven
    settlement.

    Cash accounting:
      starting_capital — equity at session open (basis for risk limits)
      available_cash   — unallocated cash; decreases as bets are placed
      total_equity     — available_cash + sum(open bet stakes) + settled P&L
    """

    def __init__(self, decision_dt: datetime, capital: float, markets: list, config: dict,
                 *, session_label: Optional[str] = None):
        if decision_dt.tzinfo is None:
            decision_dt = decision_dt.replace(tzinfo=timezone.utc)
        self.decision_dt = decision_dt
        self.year = decision_dt.year
        self.month = decision_dt.month
        # session_label is what shows up in Bet.month and report month columns.
        # Default to YYYY-MM for back-compat with monthly aggregates.
        self.month_key = session_label or decision_dt.strftime("%Y-%m")
        self.starting_capital = capital
        self.available_cash = capital
        self.markets = {m.slug: m for m in markets}
        self.bets = []
        self.trade_log = []
        self.config = config

        # Cutoff is the decision moment itself.
        self.cutoff = decision_dt
        self.cutoff_date = decision_dt.strftime("%Y-%m-%d")

    @classmethod
    def for_month(cls, year: int, month: int, capital: float, markets: list, config: dict):
        """Back-compat constructor: cutoff = end of the named month."""
        if month == 12:
            cutoff = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        else:
            cutoff = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        return cls(cutoff, capital, markets, config, session_label=f"{year}-{month:02d}")

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
        decision_naive = self.decision_dt.replace(tzinfo=None)
        for slug, m in self.markets.items():
            yes_price = m.outcome_prices[0] if m.outcome_prices else "?"
            end_str = (m.end_date or "")[:10]
            days_to_end = ""
            if end_str:
                try:
                    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
                    delta_days = (end_dt - decision_naive).days
                    days_to_end = f" | T-{delta_days}d"
                except Exception:
                    pass
            lines.append(
                f"  [{slug}] {m.question} | YES={yes_price} | vol=${m.volume:,.0f} | end={end_str}{days_to_end}"
            )
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
                "description": (
                    "在预测市场上下注。每次只下一个注。你只需要给出 model_prob (你估计的 YES 真实概率) "
                    "和 confidence (你的把握程度)，方向和金额由系统按 Kelly 公式自动决定。"
                    "如果你认为定价合理，使用 confidence='skip' 或者跳过这个市场。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "市场 slug"},
                        "model_prob": {
                            "type": "number",
                            "description": "你估计的 YES 真实概率（0-1）。"
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": (
                                "high = 你非常确信（用全 Kelly 仓位），"
                                "medium = 中等（半 Kelly），"
                                "low = 略有把握（1/4 Kelly）。"
                                "宁可低估，不要虚报。"
                            )
                        },
                        "reasoning": {"type": "string", "description": "下注理由（1-2句话）"}
                    },
                    "required": ["slug", "model_prob", "confidence", "reasoning"]
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
            args["slug"],
            args.get("model_prob"),
            args.get("confidence", "low"),
            args.get("reasoning", ""),
            ctx,
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


CONFIDENCE_KELLY_FRACTION = {
    "high": 1.0,
    "medium": 0.5,
    "low": 0.25,
}
MIN_EDGE_TO_BET = 0.03           # below 3pp edge → SKIP
MAX_BET_PCT_OF_EQUITY = 0.15     # hard cap, matches the per-bet risk limit


def _place_bet(slug: str, model_prob, confidence: str, reasoning: str,
               ctx: AgentContext) -> str:
    """Place a bet sized by Kelly given the agent's model_prob + confidence.

    The agent supplies only its belief (model_prob) and how strongly it holds
    that belief (confidence). Direction and amount fall out of Kelly math, so
    the agent can't separately game stake size — virtually pumping confidence
    just runs into the per-bet cap.
    """
    market = ctx.markets.get(slug)
    if not market:
        return json.dumps({"error": f"未找到市场: {slug}"}, ensure_ascii=False)

    market_prob = market.outcome_prices[0] if market.outcome_prices else 0.5

    if model_prob is None or not (0.0 < float(model_prob) < 1.0):
        return json.dumps({
            "error": "model_prob 必须是 0-1 之间的数字。",
        }, ensure_ascii=False)
    model_prob = max(0.01, min(0.99, float(model_prob)))

    conf_key = (confidence or "low").lower()
    conf_mult = CONFIDENCE_KELLY_FRACTION.get(conf_key)
    if conf_mult is None:
        return json.dumps({
            "error": f"confidence 必须是 high/medium/low，收到 {confidence!r}",
        }, ensure_ascii=False)

    edge = model_prob - market_prob  # signed
    abs_edge = abs(edge)
    if abs_edge < MIN_EDGE_TO_BET:
        return json.dumps({
            "status": "skipped",
            "reason": f"edge={abs_edge:.3f} 小于阈值 {MIN_EDGE_TO_BET}，定价合理，建议跳过。",
            "model_prob": round(model_prob, 4),
            "market_prob": round(market_prob, 4),
        }, ensure_ascii=False)

    if edge > 0:
        # YES is undervalued
        direction = "YES"
        kelly_raw = edge / (1 - market_prob) if market_prob < 1 else 0
    else:
        direction = "NO"
        kelly_raw = (-edge) / market_prob if market_prob > 0 else 0

    kelly_fraction = max(0.0, kelly_raw) * conf_mult
    # Cap at MAX_BET_PCT_OF_EQUITY of starting equity for risk control.
    kelly_fraction = min(kelly_fraction, MAX_BET_PCT_OF_EQUITY)

    amount = round(ctx.starting_capital * kelly_fraction, 2)
    if amount < 1.0:
        return json.dumps({
            "status": "skipped",
            "reason": f"按 Kelly 计算仓位仅 ${amount:.2f}，金额过小，建议跳过。",
            "model_prob": round(model_prob, 4),
            "market_prob": round(market_prob, 4),
            "kelly_fraction": round(kelly_fraction, 4),
        }, ensure_ascii=False)

    if amount > ctx.available_cash:
        # Scale down to whatever cash remains.
        amount = round(ctx.available_cash, 2)
        if amount < 1.0:
            return json.dumps({
                "error": "现金不足以下注。",
                "available_cash": round(ctx.available_cash, 2),
            }, ensure_ascii=False)

    from .simulator import simulate_bet

    bet = simulate_bet(
        market=market, month=ctx.month_key, direction=direction,
        model_prob=model_prob, market_prob=market_prob,
        edge=edge,
        kelly_fraction=amount / ctx.starting_capital,
        capital=ctx.starting_capital,
    )

    bet.placed_at = ctx.decision_dt.isoformat()
    bet.settle_due_at = market.end_date or None

    ctx.available_cash -= amount
    bet.resolution = None
    bet.pnl = None
    ctx.bets.append(bet)

    ctx.trade_log.append({
        "slug": slug, "direction": direction, "amount": amount,
        "model_prob": model_prob, "edge": edge, "confidence": conf_key,
        "reasoning": reasoning, "entry_price": bet.entry_price,
    })

    return json.dumps({
        "status": "ok",
        "bet_placed": f"{direction} ${amount:.2f} on [{slug}] (conf={conf_key})",
        "direction_chosen_by_system": direction,
        "amount_chosen_by_system": amount,
        "kelly_fraction": round(kelly_fraction, 4),
        "entry_price": round(bet.entry_price, 4),
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "edge": round(edge, 4),
        "available_cash": round(ctx.available_cash, 2),
        "total_equity": round(ctx.total_equity, 2),
        "note": "下注已记录。结果将在市场到期后揭晓。",
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
