"""Cross-run review for predict mode.

Before each new predict run, scan the most recent prior runs (under
runs/predict-*) and pull the current price for any market we previously
recommended a bet on. The output is a `prior_predictions_review.md` written
into the new run's output dir, surfacing how the previous calls have
moved since they were made.

This closes the predict feedback loop: without it, you generate
recommendations forever and never see whether your model_prob was right.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dateutil import parser as dateparser

log = logging.getLogger("pm-trader.predict.review")


GAMMA_BASE = "https://gamma-api.polymarket.com"


def _load_prior_runs(runs_root: Path, max_runs: int = 5) -> list[Path]:
    """Most-recent first."""
    if not runs_root.exists():
        return []
    candidates = sorted(
        [p for p in runs_root.iterdir() if p.is_dir() and p.name.startswith("predict-")
         or p.name.startswith("realpredict-")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[:max_runs]


def _load_predictions(run_dir: Path) -> list[dict]:
    """Extract bet entries from a previous run's predictions.json."""
    js_path = run_dir / "predictions.json"
    if not js_path.exists():
        return []
    try:
        with open(js_path) as f:
            data = json.load(f)
    except Exception:
        return []
    bets = data.get("bets") or []
    out = []
    for b in bets:
        if b.get("direction") in ("YES", "NO") and b.get("amount", 0) > 0:
            out.append({
                "run": run_dir.name,
                "generated_at": data.get("generated_at"),
                "slug": b.get("slug"),
                "direction": b["direction"],
                "amount": b.get("amount"),
                "model_prob": b.get("model_prob"),
                "yes_price_at_call": b.get("yes_price") or b.get("market_prob"),
                "end_date": b.get("end_date"),
                "reasoning": b.get("reasoning", ""),
                "confidence": b.get("confidence"),
            })
    return out


def _fetch_current_market(slug: str) -> Optional[dict]:
    """Hit Gamma /markets?slug= to get the live market state."""
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"slug": slug, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("gamma fetch failed for %s: %s", slug, e)
        return None
    if not data:
        return None
    raw = data[0] if isinstance(data, list) else data
    prices_raw = raw.get("outcomePrices", "[]")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    except Exception:
        prices = []
    if not prices or len(prices) < 2:
        return None
    return {
        "slug": raw.get("slug"),
        "yes_price_now": float(prices[0]),
        "no_price_now": float(prices[1]),
        "closed": bool(raw.get("closed")),
        "end_date": raw.get("endDate") or "",
        "resolution_yes": float(prices[0]) >= 0.95,
        "resolution_no": float(prices[1]) >= 0.95,
    }


def _days_until(end_date_str: str) -> Optional[int]:
    if not end_date_str:
        return None
    try:
        dt = dateparser.parse(end_date_str)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return (dt - datetime.now()).days
    except Exception:
        return None


def write_prior_predictions_review(
    runs_root: Path,
    out_dir: Path,
    max_runs: int = 3,
) -> Optional[Path]:
    """Build the cross-run review and drop it in `out_dir`.

    Returns the path to the markdown file (or None if no priors found).
    """
    prior = _load_prior_runs(runs_root, max_runs=max_runs)
    if not prior:
        log.info("No prior predict runs found.")
        return None

    all_bets: list[dict] = []
    for run_dir in prior:
        all_bets.extend(_load_predictions(run_dir))

    # De-dup by (run, slug, direction)
    seen = set()
    unique = []
    for b in all_bets:
        key = (b["run"], b["slug"], b["direction"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)

    if not unique:
        log.info("No prior bets to review.")
        return None

    log.info("Reviewing %d prior bets across %d runs", len(unique), len(prior))

    now = datetime.now()
    rows = []
    for b in unique:
        cur = _fetch_current_market(b["slug"])
        days_left = _days_until(b.get("end_date") or "")
        if cur is None:
            rows.append({**b, "current": None, "days_left": days_left})
            continue
        # Compute drift: how far did YES price move since the call
        yes_at_call = b.get("yes_price_at_call")
        yes_now = cur["yes_price_now"]
        drift = yes_now - yes_at_call if isinstance(yes_at_call, (int, float)) else None

        # Did the bet "win" if settled?
        settled_outcome = None
        if cur["closed"]:
            if cur["resolution_yes"]:
                settled_outcome = "YES"
            elif cur["resolution_no"]:
                settled_outcome = "NO"

        rows.append({
            **b,
            "yes_now": yes_now,
            "drift": drift,
            "closed_now": cur["closed"],
            "settled_outcome": settled_outcome,
            "won": (settled_outcome == b["direction"]) if settled_outcome else None,
            "days_left": days_left,
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "prior_predictions_review.md"
    json_path = out_dir / "prior_predictions_review.json"

    lines = [
        "# 历次预测回顾",
        "",
        f"**生成时间**: {now.strftime('%Y-%m-%d %H:%M')}",
        f"**回看范围**: 最近 {len(prior)} 次 predict 运行 ({prior[0].name} ... {prior[-1].name})",
        f"**追踪 bet 总数**: {len(rows)}",
        "",
        "| 运行 | slug | 方向 | model_prob | 下注时YES | 当前YES | 漂移 | T-?d | 已结算 | 命中? |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        mp = r.get("model_prob")
        mp_s = f"{mp*100:.0f}%" if isinstance(mp, (int, float)) else "?"
        at_call = r.get("yes_price_at_call")
        at_call_s = f"{at_call*100:.0f}%" if isinstance(at_call, (int, float)) else "?"
        if r.get("yes_now") is not None:
            now_s = f"{r['yes_now']*100:.0f}%"
            drift = r.get("drift")
            drift_s = f"{drift*100:+.0f}pp" if isinstance(drift, (int, float)) else "?"
        else:
            now_s = "?"
            drift_s = "?"
        days_s = f"T-{r['days_left']}d" if isinstance(r.get("days_left"), int) else "?"
        outcome = r.get("settled_outcome") or "未结算"
        hit = "—"
        if r.get("won") is True:
            hit = "✅"
        elif r.get("won") is False:
            hit = "❌"
        slug_short = r["slug"][:50]
        lines.append(
            f"| {r['run'][:20]} | {slug_short} | {r['direction']} | {mp_s} | "
            f"{at_call_s} | {now_s} | {drift_s} | {days_s} | {outcome} | {hit} |"
        )

    md_path.write_text("\n".join(lines), encoding="utf-8")

    with open(json_path, "w") as f:
        json.dump({
            "generated_at": now.isoformat(),
            "runs_reviewed": [p.name for p in prior],
            "rows": rows,
        }, f, ensure_ascii=False, indent=2, default=str)

    log.info("Prior-predictions review written to %s", md_path)
    return md_path
