"""Event-driven backtest runner.

Architecture mirrors predict mode:
  per-decision-day:
    1. fetch markets active at that point (with prices as of that point)
    2. screen down to ~10 candidates
    3. for each candidate, run an independent analyzer sub-agent
    4. translate (model_prob, confidence) into a Kelly-sized bet
       (placed_at = decision_dt, settle_due_at = market.end_date)
    5. dump per-market traces + recommendations.md/predictions.json

After the decision schedule completes:
    6. event-replay: settle every bet at its real settle_due_at, advancing cash
       in the chronological order of settlement
    7. month-bucketed reports + multi-dimension P&L analysis
"""

import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..core.types import MonthlyReport, BacktestResult, Bet
from ..core.tracer import Tracer
from ..core.llm import LLMConfig
from ..core.config import Cache
from ..core.market_data import fetch_markets_active_at
from ..core.price_data import fetch_prices_at
from ..core.market_analyzer import analyze_market
from ..core.search_backend import make_serpapi_backend
from ..core.screener import screen_markets
from ..core.report_writer import write_decision_report
from ..core.simulator import settle_bet, simulate_bet, early_close_bet
from ..core.baselines import (
    run_baseline_decision_day, BASELINE_NAMES,
)
from ..core.tools import (
    CONFIDENCE_KELLY_FRACTION, MIN_EDGE_TO_BET, MAX_BET_PCT_OF_EQUITY,
)
from ..core.reporter import generate_final_report

log = logging.getLogger("pm-backtest.runner")

CADENCE_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30}


# ─────────────────────────────────────────────────────────────────────────────
# Schedule + per-decision data
# ─────────────────────────────────────────────────────────────────────────────

def _decision_dates(start_month: str, end_month: str, cadence: str) -> list[datetime]:
    sy, sm = map(int, start_month.split("-"))
    ey, em = map(int, end_month.split("-"))
    first = datetime(sy, sm, 1, 12, 0, 0, tzinfo=timezone.utc)
    if em == 12:
        last = datetime(ey + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    else:
        last = datetime(ey, em + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    step = timedelta(days=CADENCE_DAYS.get(cadence, 7))
    out = []
    t = first
    while t <= last:
        out.append(t)
        t += step
    return out


def _decision_data(decision_dt: datetime, config: dict, cache: Cache):
    horizon_days = config["backtest"].get("horizon_days", 30)
    markets = fetch_markets_active_at(
        decision_dt,
        horizon_days=horizon_days,
        min_volume=config["backtest"]["min_monthly_volume"],
        page_limit=config["api"]["page_limit"],
        max_pages=config["api"].get("max_pages", 8),
        request_delay=config["api"]["request_delay"],
        cache=cache,
    )
    if not markets:
        return decision_dt, []

    all_tids = []
    for m in markets:
        all_tids.extend(m.token_ids)
    prices = fetch_prices_at(all_tids, decision_dt, cache=cache,
                             request_delay=config["api"]["request_delay"])

    valid_dicts = []
    for m in markets:
        yes_p = prices.get(m.token_ids[0]) if m.token_ids else None
        no_p = prices.get(m.token_ids[1]) if len(m.token_ids) > 1 else None
        if yes_p is None:
            continue
        if no_p is None:
            no_p = 1 - yes_p
        end_dt_obj = None
        try:
            from dateutil import parser as _p
            end_dt_obj = _p.parse(m.end_date)
            if end_dt_obj.tzinfo is None:
                end_dt_obj = end_dt_obj.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        days_to_end = ((end_dt_obj - decision_dt).days
                       if end_dt_obj else None)
        valid_dicts.append({
            "id": m.id,
            "slug": m.slug,
            "question": m.question,
            "category": m.category or "",
            "yes_price": round(yes_p, 4),
            "no_price": round(no_p, 4),
            "volume": m.volume,
            "end_date": m.end_date,
            "days_to_end": days_to_end,
            "_market_obj": m,  # kept for settlement lookup; stripped before screener/analyzer
        })

    return decision_dt, valid_dicts


# ─────────────────────────────────────────────────────────────────────────────
# Decision-day pipeline (mirrors predict)
# ─────────────────────────────────────────────────────────────────────────────

def _run_decision_day(
    decision_dt: datetime,
    valid_markets: list[dict],
    llm_cfg: LLMConfig,
    config: dict,
    cache: Cache,
    available_cash: float,
    starting_capital: float,
    run_dir: Path,
    parallel: int,
    journal: Optional[str] = None,
) -> tuple[list[Bet], list[dict]]:
    """One decision day: screen, analyze each candidate, return placed bets +
    full decisions list (for the report).
    """
    decision_key = decision_dt.strftime("%Y-%m-%d")
    out_dir = run_dir / "decisions" / decision_key
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = out_dir / "traces"
    traces_dir.mkdir(exist_ok=True)

    # Strip _market_obj before passing to LLM-facing components
    market_dicts_clean = [{k: v for k, v in m.items() if not k.startswith("_")}
                          for m in valid_markets]

    # Phase 1: screen
    verdicts = screen_markets(market_dicts_clean, llm_cfg, max_select=10)
    selected_slugs = [s for s, v in verdicts.items() if v["selected"]]
    log.info("  [%s] screen: %d → %d", decision_key,
             len(market_dicts_clean), len(selected_slugs))

    # Phase 2: analyze in parallel — each market gets its own sub-trace dir
    serp_search = make_serpapi_backend(
        config["api_keys"]["serpapi"]["key"], cache,
    )
    now_iso = decision_dt.strftime("%Y-%m-%d %H:%M UTC")
    cutoff_iso = decision_dt.strftime("%Y-%m-%d")
    selected_markets = [m for m in market_dicts_clean if m["slug"] in selected_slugs]

    def _analyze_with_dir(m):
        slug_safe = m["slug"].replace("/", "-")[:80]
        sub_dir = traces_dir / slug_safe
        return analyze_market(
            m, llm_cfg, serp_search,
            now_iso=now_iso, cutoff_iso=cutoff_iso,
            out_dir=sub_dir,
            research_parallel=3,
            journal=journal,
        )

    analyses: dict[str, dict] = {}
    if selected_markets:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_analyze_with_dir, m): m["slug"]
                       for m in selected_markets}
            for f in as_completed(futures):
                slug = futures[f]
                try:
                    analyses[slug] = f.result()
                except Exception as e:
                    log.error("  [%s] analyzer for %s failed: %s", decision_key, slug[:30], e)

    # Phase 3: turn (model_prob, confidence) into Kelly-sized bets
    PORTFOLIO_CAP_PCT = 0.50  # max combined exposure per decision day

    new_bets: list[Bet] = []
    decisions: list[dict] = []
    obj_by_slug = {m["slug"]: m["_market_obj"] for m in valid_markets}

    # 3a. Sketch each candidate's intended amount (pre-cap)
    candidates = []  # (decision_entry, intended_amount, direction, edge, mp)
    for m in valid_markets:
        slug = m["slug"]
        verdict = verdicts.get(slug, {"selected": False, "reason": "(no verdict)"})
        analysis = analyses.get(slug)

        if not analysis:
            decisions.append({
                "slug": slug,
                "question": m["question"],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "volume": m["volume"],
                "end_date": m["end_date"],
                "category": m.get("category", ""),
                "direction": "SKIP",
                "amount": 0.0,
                "confidence": "screened-out",
                "model_prob": None,
                "market_prob": m["yes_price"],
                "edge": None,
                "reasoning": verdict.get("reason", "(screened out)"),
                "screener_reason": verdict.get("reason", ""),
            })
            continue

        mp = analysis.get("model_prob")
        conf = analysis.get("confidence", "skip")
        yes = m["yes_price"]

        direction = "SKIP"
        intended_amount = 0.0
        edge = None
        if isinstance(mp, (int, float)):
            edge = mp - yes
            if conf in CONFIDENCE_KELLY_FRACTION and abs(edge) >= MIN_EDGE_TO_BET:
                if edge > 0:
                    direction = "YES"
                    kelly_raw = edge / (1 - yes) if yes < 1 else 0
                else:
                    direction = "NO"
                    kelly_raw = (-edge) / yes if yes > 0 else 0
                kelly = max(0.0, kelly_raw) * CONFIDENCE_KELLY_FRACTION[conf]
                kelly = min(kelly, MAX_BET_PCT_OF_EQUITY)
                intended_amount = round(starting_capital * kelly, 2)

        slug_safe = slug.replace("/", "-")[:80]
        decision_entry = {
            "slug": slug,
            "question": m["question"],
            "yes_price": m["yes_price"],
            "no_price": m["no_price"],
            "volume": m["volume"],
            "end_date": m["end_date"],
            "category": m.get("category", ""),
            "direction": direction,
            "amount": intended_amount,
            "confidence": conf,
            "model_prob": mp,
            "market_prob": yes,
            "edge": edge,
            "reasoning": analysis.get("reasoning", ""),
            "screener_reason": verdict.get("reason", ""),
            "trace_dir": f"traces/{slug_safe}",
            "research_summary": [
                {"stance": r.get("stance"), "strength": r.get("strength"),
                 "evidence_count": len(r.get("evidence", []))}
                for r in analysis.get("research", [])
            ],
            "critic": (analysis.get("critic") or {}).get("suggested_action"),
            "search_queries": analysis.get("search_queries", []),
        }
        decisions.append(decision_entry)
        candidates.append((decision_entry, intended_amount, direction, edge, mp))

    # 3b. Portfolio cap: scale all bets if total > cap, then enforce cash floor
    bet_candidates = [c for c in candidates if c[2] != "SKIP" and c[1] > 0]
    total_intended = sum(c[1] for c in bet_candidates)
    cap_total = starting_capital * PORTFOLIO_CAP_PCT
    if total_intended > cap_total > 0:
        scale = cap_total / total_intended
        for entry, _amt, _dir, _e, _mp in bet_candidates:
            entry["original_amount"] = entry["amount"]
            entry["amount"] = round(entry["amount"] * scale, 2)
            entry["scaled_down"] = True
        log.info("  [%s] portfolio cap: $%.0f → $%.0f (scale=%.2f)",
                 decision_key, total_intended, cap_total, scale)

    # 3c. Realize bets, respecting cash budget
    cash_remaining = available_cash
    for entry, _intended, direction, edge, mp in candidates:
        if direction == "SKIP":
            continue
        amount = entry["amount"]
        if amount < 1.0:
            entry["direction"] = "SKIP"
            entry["amount"] = 0.0
            continue
        if amount > cash_remaining:
            amount = round(cash_remaining, 2)
            if amount < 1.0:
                entry["direction"] = "SKIP"
                entry["amount"] = 0.0
                continue
            entry["amount"] = amount
            entry["cash_clipped"] = True

        market_obj = obj_by_slug[entry["slug"]]
        bet = simulate_bet(
            market=market_obj, month=decision_dt.strftime("%Y-%m"),
            direction=direction, model_prob=mp, market_prob=entry["market_prob"],
            edge=edge,
            kelly_fraction=amount / starting_capital,
            capital=starting_capital,
        )
        bet.placed_at = decision_dt.isoformat()
        bet.settle_due_at = market_obj.end_date or None
        bet.resolution = None
        bet.pnl = None
        new_bets.append(bet)
        cash_remaining -= amount

    # Sort decisions: bets first (by amount desc), then skips
    decisions.sort(key=lambda d: (d["direction"] == "SKIP", -d.get("amount", 0)))

    # Write recommendations.md/predictions.json
    write_decision_report(
        decisions=decisions,
        capital=starting_capital,
        out_dir=out_dir,
        title=f"Backtest Decision Day — {decision_key}",
        generated_at=now_iso,
        model_label=f"{llm_cfg.provider}/{llm_cfg.model}",
        extra_metadata={
            "decision_date": decision_key,
            "markets_available": len(valid_markets),
            "markets_screened_in": len(selected_slugs),
            "bets_placed": len(new_bets),
            "available_cash_at_open": round(available_cash, 2),
            "starting_capital": round(starting_capital, 2),
        },
    )

    return new_bets, decisions


# ─────────────────────────────────────────────────────────────────────────────
# Event replay
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        from dateutil import parser
        dt = parser.parse(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _replay_until(pending_bets: list, market_index: dict, cash: float,
                  cursor: datetime) -> tuple[float, list]:
    if not pending_bets:
        return cash, []
    pending_bets.sort(key=lambda b: _parse_iso(b.settle_due_at)
                      or datetime(2099, 1, 1, tzinfo=timezone.utc))
    newly = []
    i = 0
    while i < len(pending_bets):
        bet = pending_bets[i]
        due = _parse_iso(bet.settle_due_at)
        if due is None or due > cursor:
            i += 1
            continue
        market = market_index.get(bet.market_id)
        if market is None:
            bet.resolution = "UNRESOLVED"
            bet.pnl = 0.0
            cash += bet.amount
        else:
            settle_bet(bet, market)
            if bet.pnl is not None:
                cash += bet.amount + bet.pnl
        bet.settled_at = due.isoformat()
        newly.append(bet)
        pending_bets.pop(i)
    return cash, newly


def _early_exit_check(
    pending_bets: list, market_index: dict, cash: float,
    decision_dt: datetime, cache: Cache, config: dict,
    *, threshold: float = 0.85,
) -> tuple[float, list]:
    """For each pending bet, check the current YES price as of `decision_dt`.

    If the bet's position price (YES for YES, 1-YES for NO) is at or above
    `threshold`, close it now at the current price minus a half-spread + taker
    fee. This is the "take profit" rule.
    """
    if not pending_bets:
        return cash, []

    # Collect token_ids needed (YES side; the position we hold)
    token_ids = []
    for b in pending_bets:
        m = market_index.get(b.market_id)
        if m is None:
            continue
        if not m.token_ids:
            continue
        token_ids.append(m.token_ids[0])  # YES token

    if not token_ids:
        return cash, []

    prices = fetch_prices_at(
        token_ids, decision_dt, cache=cache,
        request_delay=config["api"].get("request_delay", 0.3),
    )

    closed = []
    i = 0
    while i < len(pending_bets):
        bet = pending_bets[i]
        m = market_index.get(bet.market_id)
        if m is None or not m.token_ids:
            i += 1
            continue
        yes_now = prices.get(m.token_ids[0])
        if yes_now is None:
            i += 1
            continue
        position_price = yes_now if bet.direction == "YES" else (1 - yes_now)
        if position_price >= threshold:
            early_close_bet(bet, yes_now, exit_at=decision_dt.isoformat())
            if bet.pnl is not None:
                cash += bet.amount + bet.pnl
            closed.append(bet)
            pending_bets.pop(i)
        else:
            i += 1
    if closed:
        log.info("  %s: early-closed %d bets", decision_dt.date(), len(closed))
    return cash, closed


# ─────────────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(config: dict, llm_cfg: LLMConfig, run_dir: str, parallel: int = 4,
                 cadence: str = "weekly", enable_journal: bool = True,
                 baseline: str = "none") -> BacktestResult:
    tracer = Tracer(run_dir)
    tracer.save_config(config)
    run_dir_path = Path(run_dir)

    cache_dir = Path(config["cache"]["dir"])
    if not cache_dir.is_absolute():
        cache_dir = Path(__file__).parent.parent.parent / cache_dir
    cache = Cache(str(cache_dir), config["cache"]["ttl_hours"])

    decision_dts = _decision_dates(
        config["backtest"]["start_month"],
        config["backtest"]["end_month"],
        cadence,
    )
    log.info("RUN | %s | %s/%s | %d decisions @ %s cadence",
             tracer.run_id, llm_cfg.provider, llm_cfg.model, len(decision_dts), cadence)

    # ── Phase 1: pre-fetch markets + prices for every decision day in parallel
    decision_markets: dict[datetime, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(parallel, len(decision_dts) or 1)) as pool:
        futures = {pool.submit(_decision_data, dt, config, cache): dt for dt in decision_dts}
        for f in as_completed(futures):
            dt, valid = f.result()
            decision_markets[dt] = valid
            log.info("  %s: %d markets", dt.date(), len(valid))

    # ── Phase 2: per-decision-day pipeline + chronological cash ─────────────
    initial_capital = config["backtest"]["initial_capital"]
    pending_bets: list[Bet] = []
    settled_bets: list[Bet] = []
    market_index: dict[str, object] = {}
    cash = initial_capital
    all_decisions_by_day: dict[str, list[dict]] = {}

    early_exit_threshold = config["backtest"].get("early_exit_threshold", 0.85)
    enable_early_exit = config["backtest"].get("enable_early_exit", True)

    for dt in sorted(decision_dts):
        # Replay any settlements between last decision and now
        cash, newly_settled = _replay_until(pending_bets, market_index, cash, dt)
        settled_bets.extend(newly_settled)

        # Early exit: any pending bet whose current YES price has moved into
        # the take-profit zone gets closed at decision-day prices.
        if enable_early_exit and pending_bets:
            cash, newly_closed = _early_exit_check(
                pending_bets, market_index, cash, dt, cache, config,
                threshold=early_exit_threshold,
            )
            settled_bets.extend(newly_closed)

        valid = decision_markets.get(dt, [])
        for m in valid:
            market_index.setdefault(m["id"], m["_market_obj"])

        if not valid:
            log.info("  %s: skipping (no tradable markets)", dt.date())
            continue
        if cash <= 0:
            log.warning("  %s: out of cash, halting", dt.date())
            break

        open_stake_total = sum(b.amount for b in pending_bets)
        starting_capital_session = cash + open_stake_total

        new_bets, day_decisions = _run_decision_day(
            decision_dt=dt,
            valid_markets=valid,
            llm_cfg=llm_cfg,
            config=config,
            cache=cache,
            available_cash=cash,
            starting_capital=starting_capital_session,
            run_dir=run_dir_path,
            parallel=parallel,
            journal=_build_journal(settled_bets) if enable_journal else None,
        )
        all_decisions_by_day[dt.strftime("%Y-%m-%d")] = day_decisions
        for b in new_bets:
            pending_bets.append(b)
            cash -= b.amount
        log.info("  %s: placed %d bets, cash %.2f", dt.date(), len(new_bets), cash)

    # Final replay
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    cash, tail = _replay_until(pending_bets, market_index, cash, far_future)
    settled_bets.extend(tail)

    # ── Phase 3: month aggregation + multi-dimensional analysis ────────────
    monthly_reports = _aggregate_by_settle_month(
        settled_bets, pending_bets, initial_capital, cash,
        end_month=config["backtest"].get("end_month"),
    )
    result = generate_final_report(monthly_reports, initial_capital)

    # Multi-dimension analysis
    from ..core.analysis import write_analysis_report
    write_analysis_report(
        run_dir=run_dir_path,
        settled=settled_bets,
        pending=pending_bets,
        decisions_by_day=all_decisions_by_day,
        initial_capital=initial_capital,
        final_capital=cash,
    )

    tracer.save_result({
        "total_pnl": result.total_pnl, "total_roi": result.total_roi,
        "sharpe_ratio": result.sharpe_ratio, "max_drawdown": result.max_drawdown,
        "total_bets": result.total_bets, "overall_win_rate": result.overall_win_rate,
        "months": [{"month": r.month, "bets": r.total_bets, "won": r.won,
                    "lost": r.lost, "pnl": r.total_pnl, "roi": r.roi,
                    "capital": r.ending_capital} for r in monthly_reports]
    })

    # ── Phase 4: optional baseline runs for comparison ─────────────────
    baseline_summaries: dict[str, dict] = {}
    if baseline and baseline != "none":
        names_to_run = BASELINE_NAMES if baseline == "all" else [baseline]
        for name in names_to_run:
            log.info("BASELINE | running %s", name)
            bl_summary = _run_baseline_track(
                name=name,
                decision_dts=sorted(decision_dts),
                decision_markets=decision_markets,
                initial_capital=initial_capital,
                run_dir_root=run_dir_path,
                cache=cache,
                config=config,
            )
            baseline_summaries[name] = bl_summary

        # Write a comparison file at run root for easy diffing
        _write_baseline_comparison(
            run_dir=run_dir_path,
            llm_summary={
                "name": "llm-agent",
                "total_pnl": result.total_pnl,
                "total_roi": result.total_roi,
                "total_bets": result.total_bets,
                "win_rate": result.overall_win_rate,
                "sharpe": result.sharpe_ratio,
                "max_drawdown": result.max_drawdown,
                "final_capital": cash,
            },
            baseline_summaries=baseline_summaries,
            initial_capital=initial_capital,
        )

    tracer.close()
    return result


def _run_baseline_track(
    *,
    name: str,
    decision_dts: list[datetime],
    decision_markets: dict[datetime, list[dict]],
    initial_capital: float,
    run_dir_root: Path,
    cache: Cache,
    config: dict,
) -> dict:
    """Replay the same decision schedule with a baseline strategy. No LLM."""
    out_dir = run_dir_root / "baselines" / name
    out_dir.mkdir(parents=True, exist_ok=True)

    early_exit_threshold = config["backtest"].get("early_exit_threshold", 0.85)
    enable_early_exit = config["backtest"].get("enable_early_exit", True)

    pending_bets: list[Bet] = []
    settled_bets: list[Bet] = []
    market_index: dict[str, object] = {}
    cash = initial_capital
    decisions_by_day: dict[str, list[dict]] = {}

    for seed_idx, dt in enumerate(decision_dts):
        cash, newly = _replay_until(pending_bets, market_index, cash, dt)
        settled_bets.extend(newly)
        if enable_early_exit and pending_bets:
            cash, closed = _early_exit_check(
                pending_bets, market_index, cash, dt, cache, config,
                threshold=early_exit_threshold,
            )
            settled_bets.extend(closed)

        valid = decision_markets.get(dt, [])
        for m in valid:
            market_index.setdefault(m["id"], m["_market_obj"])
        if not valid or cash <= 0:
            continue

        market_dicts_clean = [{k: v for k, v in m.items() if not k.startswith("_")}
                              for m in valid]
        obj_by_slug = {m["slug"]: m["_market_obj"] for m in valid}
        open_stake_total = sum(b.amount for b in pending_bets)
        starting_capital_session = cash + open_stake_total

        new_bets, day_decisions = run_baseline_decision_day(
            name=name,
            decision_dt=dt,
            valid_markets=market_dicts_clean,
            available_cash=cash,
            starting_capital=starting_capital_session,
            obj_by_slug=obj_by_slug,
            seed=seed_idx,
        )
        decisions_by_day[dt.strftime("%Y-%m-%d")] = day_decisions
        for b in new_bets:
            pending_bets.append(b)
            cash -= b.amount

    # Tail settle
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    cash, tail = _replay_until(pending_bets, market_index, cash, far_future)
    settled_bets.extend(tail)

    monthly_reports = _aggregate_by_settle_month(
        settled_bets, pending_bets, initial_capital, cash,
        end_month=config["backtest"].get("end_month"),
    )
    result = generate_final_report(monthly_reports, initial_capital)

    # Per-baseline analysis report
    from ..core.analysis import write_analysis_report
    write_analysis_report(
        run_dir=out_dir,
        settled=settled_bets,
        pending=pending_bets,
        decisions_by_day=decisions_by_day,
        initial_capital=initial_capital,
        final_capital=cash,
    )

    return {
        "name": name,
        "total_pnl": result.total_pnl,
        "total_roi": result.total_roi,
        "total_bets": result.total_bets,
        "win_rate": result.overall_win_rate,
        "sharpe": result.sharpe_ratio,
        "max_drawdown": result.max_drawdown,
        "final_capital": cash,
    }


def _write_baseline_comparison(
    *, run_dir: Path, llm_summary: dict, baseline_summaries: dict,
    initial_capital: float,
) -> None:
    rows = [llm_summary] + list(baseline_summaries.values())
    lines = [
        "# Baseline Comparison",
        "",
        f"Initial capital: ${initial_capital:,.2f}",
        "",
        "| strategy | final | P&L | ROI | bets | win% | Sharpe | MaxDD |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | ${r['final_capital']:,.2f} | "
            f"${r['total_pnl']:+,.2f} | {r['total_roi']*100:+.1f}% | "
            f"{r['total_bets']} | {r['win_rate']*100:.0f}% | "
            f"{r['sharpe']:.2f} | {r['max_drawdown']*100:.1f}% |"
        )
    (run_dir / "baseline_comparison.md").write_text("\n".join(lines), encoding="utf-8")
    import json as _json
    (run_dir / "baseline_comparison.json").write_text(
        _json.dumps({"llm": llm_summary, "baselines": baseline_summaries}, indent=2),
        encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Settlement-time month bucketing
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_by_settle_month(settled, still_pending, initial_capital, final_cash,
                                 end_month: Optional[str] = None):
    """Bucket settled bets by their settled_at YYYY-MM month.

    Bets that settled after `end_month` are placed in an `out-of-window`
    bucket and reported separately so the headline P&L can distinguish
    "the model called it during the backtest period" from "long-tail bets
    that happened to land later".
    """
    by_month: dict[str, list] = defaultdict(list)
    out_of_window: list = []
    for b in settled:
        if not b.settled_at:
            by_month["unresolved"].append(b)
            continue
        bucket = b.settled_at[:7]
        if end_month and bucket > end_month:
            out_of_window.append(b)
        else:
            by_month[bucket].append(b)
    months_sorted = sorted(k for k in by_month.keys() if k != "unresolved")
    reports: list[MonthlyReport] = []
    capital = initial_capital
    for mk in months_sorted:
        bets = by_month[mk]
        won = [b for b in bets if (b.pnl or 0) > 0]
        lost = [b for b in bets if (b.pnl or 0) < 0]
        total_pnl = sum(b.pnl or 0 for b in bets)
        ending = capital + total_pnl
        reports.append(MonthlyReport(
            month=mk, total_bets=len(bets), won=len(won), lost=len(lost),
            unresolved=0, win_rate=len(won) / len(bets) if bets else 0,
            total_bet_amount=sum(b.amount for b in bets),
            total_pnl=total_pnl, starting_capital=capital, ending_capital=ending,
            roi=total_pnl / capital if capital > 0 else 0, bets=bets,
        ))
        capital = ending
    if out_of_window:
        won = [b for b in out_of_window if (b.pnl or 0) > 0]
        lost = [b for b in out_of_window if (b.pnl or 0) < 0]
        total_pnl = sum(b.pnl or 0 for b in out_of_window)
        ending = capital + total_pnl
        reports.append(MonthlyReport(
            month="out-of-window", total_bets=len(out_of_window),
            won=len(won), lost=len(lost), unresolved=0,
            win_rate=len(won) / len(out_of_window) if out_of_window else 0,
            total_bet_amount=sum(b.amount for b in out_of_window),
            total_pnl=total_pnl, starting_capital=capital, ending_capital=ending,
            roi=total_pnl / capital if capital > 0 else 0, bets=out_of_window,
        ))
        capital = ending
    if still_pending:
        reports.append(MonthlyReport(
            month="pending", total_bets=len(still_pending), won=0, lost=0,
            unresolved=len(still_pending), win_rate=0,
            total_bet_amount=sum(b.amount for b in still_pending),
            total_pnl=0, starting_capital=capital, ending_capital=capital,
            roi=0, bets=still_pending,
        ))
    return reports


def _build_journal(settled_bets: list, max_entries: int = 8) -> Optional[str]:
    """Render a compact retrospect of recent settled bets for agent context.

    Returns None if there's nothing to surface yet (first decision day).
    Each line: slug | direction (model_prob → market_prob) | result | pnl.
    Last `max_entries` chronologically.
    """
    if not settled_bets:
        return None
    sorted_bets = sorted(
        (b for b in settled_bets if b.settled_at),
        key=lambda b: b.settled_at,
    )
    recent = sorted_bets[-max_entries:]
    if not recent:
        return None
    lines = []
    for b in recent:
        mp = f"{b.model_prob:.0%}" if b.model_prob is not None else "?"
        mkt = f"{b.market_prob:.0%}" if b.market_prob is not None else "?"
        outcome = b.resolution or "?"
        pnl = f"${b.pnl:+.2f}" if b.pnl is not None else "?"
        lines.append(
            f"- [{b.market_id[:30]}] {b.direction} (你估 {mp} vs 市场 {mkt}) → "
            f"结果={outcome}, P&L={pnl}"
        )
    return "\n".join(lines)
