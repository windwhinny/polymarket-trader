"""Real-time prediction — analyze active markets, output bet recommendations.

Pipeline:
  1. fetch active markets (Gamma)
  2. screen down to a manageable subset (one screener LLM call)
  3. for each selected market, run an independent analyzer sub-agent
  4. write per-market traces + consolidated recommendations.md/predictions.json
"""

import json
import logging
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from dateutil import parser as dateparser

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core.llm import LLMConfig
from src.core.config import Cache
from src.core.market_analyzer import analyze_market
from src.core.search_backend import make_tavily_backend
from src.core.screener import screen_markets
from src.core.report_writer import write_decision_report

log = logging.getLogger("pm-trader.predict")


def _fetch_active_markets(min_volume: float = 5000, limit: int = 50,
                          cache: Cache = None) -> list[dict]:
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
                        "offset": offset, "order": "volumeNum", "ascending": "false"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error("Gamma API error: %s", e)
            break

        now = datetime.now()
        for raw in data:
            outcomes_raw = raw.get("outcomes", "[]")
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            if len(outcomes) != 2:
                continue
            prices_raw = raw.get("outcomePrices", "[]")
            prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            if not prices or len(prices) < 2:
                continue
            vol = float(raw.get("volume", 0))
            if vol < min_volume:
                continue

            end_date = raw.get("endDate", "")
            if not end_date:
                continue
            try:
                end_dt = dateparser.parse(end_date)
            except Exception:
                end_dt = None
            if end_dt is None:
                continue
            if end_dt.tzinfo is not None:
                end_dt = end_dt.replace(tzinfo=None)
            if end_dt > now + timedelta(days=90) or end_dt < now:
                continue

            days_to_end = (end_dt - now).days

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
                "days_to_end": days_to_end,
            })

        if len(data) < limit:
            break

    if cache:
        cache.set(all_markets, *cache_key)

    return all_markets


def run_predict(config: dict, llm_cfg: LLMConfig, output_dir: str,
                capital: float = 1000, min_volume: float = 10000,
                parallel: int = 4) -> dict:
    """Run real-time predictions — screen markets, then analyze in parallel."""
    cache_dir = Path(config["cache"]["dir"])
    if not cache_dir.is_absolute():
        cache_dir = Path(__file__).parent.parent.parent / cache_dir
    cache = Cache(str(cache_dir), 1)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Phase 0: review prior runs (cross-run feedback loop)
    try:
        from src.predict.review import write_prior_predictions_review
        runs_root = out.parent
        write_prior_predictions_review(runs_root=runs_root, out_dir=out, max_runs=3)
    except Exception as e:
        log.warning("prior-prediction review failed: %s", e)

    log.info("Fetching active markets (vol >= $%.0f)...", min_volume)
    all_markets = _fetch_active_markets(min_volume=min_volume, limit=50, cache=cache)
    log.info("Got %d markets total", len(all_markets))

    # Phase 1: screen
    verdicts = screen_markets(all_markets, llm_cfg, max_select=10)

    # Phase 2: analyze each selected market in parallel
    selected = [m for m in all_markets if verdicts.get(m["slug"], {}).get("selected")]
    log.info("Phase 2: %d markets to analyze (workers=%d)", len(selected), parallel)

    traces_root = out / "traces"
    traces_root.mkdir(exist_ok=True)

    tavily_search = make_tavily_backend(config["api_keys"]["tavily"]["key"])
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _analyze_with_dir(m):
        slug_safe = m["slug"].replace("/", "-")[:80]
        sub_dir = traces_root / slug_safe
        return analyze_market(
            m, llm_cfg, tavily_search,
            now_iso=now_iso, cutoff_iso=None,
            out_dir=sub_dir,
            research_parallel=3,
        )

    analyses: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(_analyze_with_dir, m): m["slug"] for m in selected}
        for f in as_completed(futures):
            slug = futures[f]
            try:
                analyses[slug] = f.result()
            except Exception as e:
                log.error("[%s] analysis failed: %s", slug[:30], e)
                analyses[slug] = None

    decisions = _build_decisions(all_markets, verdicts, analyses, capital, out)
    _save_summary(decisions, capital, out, llm_cfg, now_iso)

    return {"markets_analyzed": len(selected), "output_dir": str(out)}


def _build_decisions(all_markets, verdicts, analyses, capital, out_dir: Path) -> list[dict]:
    """Merge screener verdicts with analyzer outputs into a unified per-market list.

    Per-market traces are already written by the analyzer sub-agent into
    `out_dir/traces/{slug}/`; here we just reference them.
    """
    from src.core.tools import (
        CONFIDENCE_KELLY_FRACTION, MIN_EDGE_TO_BET, MAX_BET_PCT_OF_EQUITY,
    )

    decisions = []
    for m in all_markets:
        slug = m["slug"]
        verdict = verdicts.get(slug, {"selected": False, "reason": "(no verdict)"})
        analysis = analyses.get(slug) if verdict["selected"] else None

        if analysis:
            mp = analysis.get("model_prob") or m["yes_price"]
            conf = analysis.get("confidence", "skip")
            yes = m["yes_price"]
            edge = mp - yes  # signed
            direction = "SKIP"
            amount = 0.0
            if conf in CONFIDENCE_KELLY_FRACTION and abs(edge) >= MIN_EDGE_TO_BET:
                direction = "YES" if edge > 0 else "NO"
                kelly_raw = (edge / (1 - yes)) if edge > 0 else ((-edge) / yes)
                kelly = max(0.0, kelly_raw) * CONFIDENCE_KELLY_FRACTION[conf]
                kelly = min(kelly, MAX_BET_PCT_OF_EQUITY)
                amount = round(capital * kelly, 2)

            slug_safe = slug.replace("/", "-")[:80]
            decisions.append({
                "slug": slug,
                "question": m["question"],
                "yes_price": yes,
                "no_price": m["no_price"],
                "volume": m["volume"],
                "end_date": m["end_date"],
                "category": m.get("category", ""),
                "direction": direction,
                "amount": amount,
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
            })
        else:
            # Skipped at screener stage — surface why.
            decisions.append({
                "slug": slug,
                "question": m["question"],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "volume": m["volume"],
                "end_date": m["end_date"],
                "category": m.get("category", ""),
                "direction": "SKIP",
                "amount": 0,
                "confidence": "screened-out",
                "model_prob": None,
                "market_prob": m["yes_price"],
                "edge": None,
                "reasoning": verdict.get("reason", "(screened out)"),
                "screener_reason": verdict.get("reason", ""),
            })
    return decisions


def _save_summary(decisions, capital, out_dir, llm_cfg, generated_at):
    # Cross-market portfolio cap
    bets = [d for d in decisions if d["direction"] != "SKIP"]
    PORTFOLIO_CAP_PCT = 0.50
    cap_total = capital * PORTFOLIO_CAP_PCT
    requested = sum(d["amount"] for d in bets)
    if requested > cap_total > 0:
        scale = cap_total / requested
        for d in bets:
            d["original_amount"] = d["amount"]
            d["amount"] = round(d["amount"] * scale, 2)
            d["scaled_down"] = True

    decisions.sort(key=lambda d: (d["direction"] == "SKIP", -d.get("amount", 0)))

    write_decision_report(
        decisions=decisions,
        capital=capital,
        out_dir=out_dir,
        title="Polymarket 实时预测报告",
        generated_at=generated_at,
        model_label=f"{llm_cfg.provider}/{llm_cfg.model}",
    )
