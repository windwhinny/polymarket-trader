"""Unified runner — orchestrates one backtest run with full tracing."""

import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.types import MonthlyReport, BacktestResult
from ..core.tracer import Tracer
from ..core.llm import LLMClient, LLMConfig
from ..core.config import Cache
from ..core.market_data import fetch_markets
from ..core.price_data import fetch_prices_at_month_end
from ..core.tools import AgentContext, build_tools, execute_tool
from ..core.agent import SYSTEM_PROMPT_TEMPLATE
from ..core.reporter import generate_final_report

log = logging.getLogger("pm-backtest.runner")


def _month_data(year: int, month: int, config: dict, cache: Cache):
    markets = fetch_markets(year, month,
        min_volume=config["backtest"]["min_monthly_volume"],
        page_limit=config["api"]["page_limit"],
        max_pages=config["api"].get("max_pages", 8),
        request_delay=config["api"]["request_delay"], cache=cache)

    if not markets:
        return year, month, [], {}

    all_tids = []
    for m in markets:
        all_tids.extend(m.token_ids)
    prices = fetch_prices_at_month_end(all_tids, year, month, cache=cache,
                                        request_delay=config["api"]["request_delay"])

    for m in markets:
        yes_p = prices.get(m.token_ids[0]) if m.token_ids else None
        no_p = prices.get(m.token_ids[1]) if len(m.token_ids) > 1 else None
        if yes_p is not None and no_p is not None:
            m.outcome_prices = [round(yes_p, 4), round(no_p, 4)]
        elif yes_p is not None:
            m.outcome_prices = [round(yes_p, 4), round(1 - yes_p, 4)]

    valid = [m for m in markets if m.outcome_prices and len(m.outcome_prices) == 2]
    return year, month, valid, prices


def _run_agent_traced(ctx: AgentContext, llm_cfg: LLMConfig, tracer: Tracer) -> AgentContext:
    """Run agent with full tracing."""
    client = LLMClient(llm_cfg)

    system_msg = SYSTEM_PROMPT_TEMPLATE.format(
        month=f"{ctx.year}年{ctx.month}月",
        month_num=ctx.month,
        capital=ctx.capital,
        cutoff_date=ctx.cutoff_date,
        markets=ctx.market_summary(),
    )
    tracer.system(system_msg)

    messages = [{"role": "system", "content": system_msg}]
    tools = build_tools()

    for turn in range(1, 31):
        tracer.turn_start(turn)
        tracer.model_call(len(messages), llm_cfg.provider, llm_cfg.model, 0.3)

        try:
            content, tool_calls, reasoning = client.chat(messages, tools, temperature=0.3, max_tokens=1000)
        except Exception as e:
            log.error("API ERROR turn %d: %s", turn, e)
            tracer.error(str(e))
            break

        tracer.model_response(content, tool_calls)

        if tool_calls:
            for tc in tool_calls:
                func_name = tc["name"]
                func_args = tc.get("parsed_args", {})
                if not func_args:
                    try:
                        func_args = json.loads(tc.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        func_args = {}

                tracer.tool_call(func_name, func_args)
                result = execute_tool(func_name, func_args, ctx)
                tracer.tool_result(func_name, result)

                # Record bets
                if func_name == "place_bet":
                    latest_bet = ctx.bets[-1] if ctx.bets else None
                    if latest_bet:
                        tracer.bet(
                            ctx.month_key, latest_bet.direction, latest_bet.amount,
                            func_args.get("slug", ""), latest_bet.pnl or 0,
                            latest_bet.resolution or "?", func_args.get("reasoning", ""),
                        )

                assistant_msg = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": func_name, "arguments": tc.get("arguments", "{}")}
                    }]
                }
                if reasoning:
                    assistant_msg["reasoning_content"] = reasoning
                messages.append(assistant_msg)
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"], "content": result,
                })

                if func_name == "finish_trading":
                    tracer.finish(ctx.month_key, ctx.capital,
                                  func_args.get("strategy_summary", ""),
                                  func_args.get("key_decisions", ""))
                    log.info("AGENT DONE | %s | %d bets | $%.2f→$%.2f",
                             ctx.month_key, len(ctx.bets), ctx.starting_capital, ctx.capital)
                    return ctx

        elif content:
            assistant_msg = {"role": "assistant", "content": content}
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)

    log.warning("AGENT MAX TURNS | %s", ctx.month_key)
    return ctx


def run_backtest(config: dict, llm_cfg: LLMConfig, run_dir: str, parallel: int = 4) -> BacktestResult:
    tracer = Tracer(run_dir)
    tracer.save_config(config)

    cache_dir = Path(config["cache"]["dir"])
    if not cache_dir.is_absolute():
        cache_dir = Path(__file__).parent.parent.parent / cache_dir  # backtest/runner.py → ../../
    cache = Cache(str(cache_dir), config["cache"]["ttl_hours"])

    sy, sm = map(int, config["backtest"]["start_month"].split("-"))
    ey, em = map(int, config["backtest"]["end_month"].split("-"))
    months = []
    y, m = sy, sm
    while (y < ey) or (y == ey and m <= em):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1; y += 1

    log.info("RUN | %s | %s/%s | %d months",
             tracer.run_id, llm_cfg.provider, llm_cfg.model, len(months))

    # Phase 1: Fetch data
    all_data = {}
    with ThreadPoolExecutor(max_workers=min(parallel, len(months))) as pool:
        futures = {pool.submit(_month_data, y, m, config, cache): (y, m) for y, m in months}
        for f in as_completed(futures):
            y, m, valid, _ = f.result()
            all_data[(y, m)] = valid
            log.info("  %d-%02d: %d markets", y, m, len(valid))

    # Phase 2: Run agents (compounding)
    capital = config["backtest"]["initial_capital"]
    monthly_reports = []

    for (y, m), valid in sorted(all_data.items()):
        ctx = AgentContext(y, m, capital, valid, config)
        ctx = _run_agent_traced(ctx, llm_cfg, tracer)

        # Post-agent: settle all bets against known resolutions
        from ..core.simulator import settle_bet
        for bet in ctx.bets:
            market = ctx.markets.get(bet.market_id)
            if market is None:
                # Try to find by slug
                for mk in valid:
                    if mk.id == bet.market_id:
                        market = mk
                        break
            if market:
                settle_bet(bet, market)
                if bet.pnl is not None:
                    ctx.capital += bet.amount + bet.pnl  # return stake + profit/loss

        settled = [b for b in ctx.bets if b.pnl is not None]
        report = MonthlyReport(
            month=ctx.month_key,
            total_bets=len(ctx.bets),
            won=len([b for b in settled if b.pnl > 0]),
            lost=len([b for b in settled if b.pnl < 0]),
            unresolved=len([b for b in ctx.bets if b.pnl is None]),
            win_rate=len([b for b in settled if b.pnl > 0]) / len(settled) if settled else 0,
            total_bet_amount=sum(b.amount for b in ctx.bets),
            total_pnl=ctx.capital - capital,
            starting_capital=capital,
            ending_capital=ctx.capital,
            roi=(ctx.capital - capital) / capital if capital > 0 else 0,
            bets=ctx.bets,
        )
        capital = ctx.capital
        monthly_reports.append(report)

    result = generate_final_report(monthly_reports, config["backtest"]["initial_capital"])
    tracer.save_result({
        "total_pnl": result.total_pnl, "total_roi": result.total_roi,
        "sharpe_ratio": result.sharpe_ratio, "max_drawdown": result.max_drawdown,
        "total_bets": result.total_bets, "overall_win_rate": result.overall_win_rate,
        "months": [{"month": r.month, "bets": r.total_bets, "won": r.won,
                    "lost": r.lost, "pnl": r.total_pnl, "roi": r.roi,
                    "capital": r.ending_capital} for r in monthly_reports]
    })
    tracer.close()
    return result
