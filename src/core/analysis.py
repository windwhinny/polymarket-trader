"""Multi-dimension P&L analysis for backtest runs.

Slices: by confidence, by category, calibration buckets, cumulative PnL series.
Outputs: analysis.md (human readable), analysis.json (machine readable).
"""

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("pm-trader.analysis")


def _bucket_stats(bets: list) -> dict:
    if not bets:
        return {"n": 0}
    won = [b for b in bets if (b.pnl or 0) > 0]
    lost = [b for b in bets if (b.pnl or 0) < 0]
    total_amount = sum(b.amount for b in bets)
    total_pnl = sum(b.pnl or 0 for b in bets)
    return {
        "n": len(bets),
        "won": len(won),
        "lost": len(lost),
        "win_rate": round(len(won) / len(bets), 4) if bets else 0,
        "total_amount": round(total_amount, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": round(total_pnl / total_amount, 4) if total_amount > 0 else 0,
        "avg_edge": round(sum(b.edge or 0 for b in bets) / len(bets), 4),
        "avg_amount": round(total_amount / len(bets), 2),
    }


def _confidence_for_bet(bet, decisions_by_day: dict) -> Optional[str]:
    """Look up the confidence the analyzer assigned to this bet."""
    placed_day = (bet.placed_at or "")[:10]
    decisions = decisions_by_day.get(placed_day, [])
    for d in decisions:
        if d.get("amount", 0) > 0 and d.get("direction") == bet.direction:
            # Match by direction + amount, since slug isn't on Bet directly.
            # Fall back to first matching amount if multiple.
            if abs(d.get("amount", 0) - bet.amount) < 0.01:
                return d.get("confidence")
    return None


def _category_for_bet(bet, decisions_by_day: dict) -> Optional[str]:
    placed_day = (bet.placed_at or "")[:10]
    decisions = decisions_by_day.get(placed_day, [])
    for d in decisions:
        if abs(d.get("amount", 0) - bet.amount) < 0.01 and d.get("direction") == bet.direction:
            return d.get("category", "") or "Other"
    return None


def _calibration_buckets(bets: list) -> list[dict]:
    """For each model_prob bucket [0-10%, 10-20%, ...], compute the
    realized win rate. Bets are normalized so model_prob is the probability
    of the side the agent picked actually winning.

    Win definition: a bet "won" if pnl > 0. This handles both natural
    settlement and early-close exits uniformly.
    """
    buckets: dict[int, list] = defaultdict(list)
    for b in bets:
        if b.model_prob is None or b.pnl is None:
            continue
        # The agent's stated belief about THE EVENT IT BET ON happening:
        #   YES bet → model_prob (P(YES))
        #   NO  bet → 1 - model_prob (P(NO))
        belief = b.model_prob if b.direction == "YES" else (1 - b.model_prob)
        won = (b.pnl or 0) > 0
        idx = min(9, int(belief * 10))
        buckets[idx].append((belief, won))
    out = []
    for i in range(10):
        items = buckets[i]
        if not items:
            out.append({"range": f"{i*10}-{(i+1)*10}%", "n": 0,
                        "avg_belief": None, "actual_win_rate": None})
            continue
        avg_b = sum(x[0] for x in items) / len(items)
        actual = sum(1 for x in items if x[1]) / len(items)
        out.append({
            "range": f"{i*10}-{(i+1)*10}%",
            "n": len(items),
            "avg_belief": round(avg_b, 4),
            "actual_win_rate": round(actual, 4),
        })
    return out


def _cumulative_pnl_series(settled: list, initial_capital: float) -> list[dict]:
    """Time-ordered cumulative P&L series, keyed by settled_at."""
    rows = sorted(
        (b for b in settled if b.settled_at and b.pnl is not None),
        key=lambda b: b.settled_at,
    )
    capital = initial_capital
    series = [{"t": "start", "capital": round(capital, 2), "pnl": 0.0}]
    for b in rows:
        capital += b.pnl or 0
        series.append({
            "t": b.settled_at[:10],
            "capital": round(capital, 2),
            "pnl": round(b.pnl or 0, 2),
            "direction": b.direction,
            "amount": round(b.amount, 2),
            "edge": round(b.edge or 0, 4),
            "model_prob": round(b.model_prob, 4) if b.model_prob is not None else None,
        })
    return series


def write_analysis_report(
    run_dir: Path,
    settled: list,
    pending: list,
    decisions_by_day: dict[str, list[dict]],
    initial_capital: float,
    final_capital: float,
) -> None:
    """Compute slices and write analysis.md + analysis.json."""
    all_bets = list(settled) + list(pending)
    settled_resolved = [b for b in settled if b.resolution not in (None, "UNRESOLVED")]

    # Slice: by confidence
    by_conf: dict[str, list] = defaultdict(list)
    for b in settled_resolved:
        conf = _confidence_for_bet(b, decisions_by_day) or "unknown"
        by_conf[conf].append(b)
    confidence_stats = {k: _bucket_stats(v) for k, v in by_conf.items()}

    # Slice: by category
    by_cat: dict[str, list] = defaultdict(list)
    for b in settled_resolved:
        cat = _category_for_bet(b, decisions_by_day) or "Other"
        by_cat[cat].append(b)
    category_stats = {k: _bucket_stats(v) for k, v in by_cat.items()}

    # Calibration
    calibration = _calibration_buckets(settled_resolved)

    # Cumulative PnL
    pnl_series = _cumulative_pnl_series(settled, initial_capital)

    overall = _bucket_stats(settled_resolved)
    total_pnl = round(final_capital - initial_capital, 2)
    total_roi = round(total_pnl / initial_capital, 4) if initial_capital > 0 else 0

    payload = {
        "summary": {
            "initial_capital": round(initial_capital, 2),
            "final_capital": round(final_capital, 2),
            "total_pnl": total_pnl,
            "total_roi": total_roi,
            "total_bets_placed": len(all_bets),
            "settled": len(settled_resolved),
            "pending": len(pending),
        },
        "overall": overall,
        "by_confidence": confidence_stats,
        "by_category": category_stats,
        "calibration": calibration,
        "pnl_series": pnl_series,
    }

    json_path = run_dir / "analysis.json"
    with open(json_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    md_path = run_dir / "analysis.md"
    md_path.write_text(_render_analysis_md(payload), encoding="utf-8")
    log.info("Analysis: %s", md_path)


def _render_analysis_md(p: dict) -> str:
    s = p["summary"]
    lines = [
        "# Backtest Analysis",
        "",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 总览",
        "",
        f"- 起始资金: ${s['initial_capital']:,.2f}",
        f"- 最终资金: ${s['final_capital']:,.2f}",
        f"- 总 P&L: ${s['total_pnl']:,.2f}",
        f"- 总 ROI: {s['total_roi']*100:.1f}%",
        f"- 下注笔数: {s['total_bets_placed']}（已结算 {s['settled']} / 未结算 {s['pending']}）",
        "",
        "## 已结算总体",
        "",
    ]
    o = p["overall"]
    if o.get("n"):
        lines += [
            f"- 数量: {o['n']}",
            f"- 胜率: {o['win_rate']*100:.1f}% ({o['won']}/{o['n']})",
            f"- 总投入: ${o['total_amount']:,.2f}",
            f"- 总 P&L: ${o['total_pnl']:,.2f}",
            f"- 平均 ROI: {o['roi']*100:.1f}%",
            f"- 平均下注 edge: {o['avg_edge']*100:+.1f}%",
            f"- 平均下注金额: ${o['avg_amount']:.2f}",
        ]
    else:
        lines.append("(无已结算下注)")

    lines += ["", "## 按 confidence 分桶", "", "| confidence | n | 胜率 | 总 P&L | ROI | 平均 edge |",
              "|---|---|---|---|---|---|"]
    for conf in ("high", "medium", "low", "unknown"):
        st = p["by_confidence"].get(conf)
        if not st or not st.get("n"):
            continue
        lines.append(f"| {conf} | {st['n']} | {st['win_rate']*100:.1f}% | ${st['total_pnl']:,.2f} "
                     f"| {st['roi']*100:.1f}% | {st['avg_edge']*100:+.1f}% |")

    lines += ["", "## 按 category 分桶", "", "| category | n | 胜率 | 总 P&L | ROI |",
              "|---|---|---|---|---|"]
    for cat, st in sorted(p["by_category"].items(), key=lambda x: -x[1].get("n", 0)):
        if not st.get("n"):
            continue
        lines.append(f"| {cat or '-'} | {st['n']} | {st['win_rate']*100:.1f}% | ${st['total_pnl']:,.2f} | {st['roi']*100:.1f}% |")

    lines += ["", "## 校准曲线 (model_prob → 实际胜率)",
              "",
              "| model_prob 区间 | n | 平均 belief | 实际胜率 | 偏差 |",
              "|---|---|---|---|---|"]
    for b in p["calibration"]:
        if b["n"] == 0:
            continue
        bias = b["avg_belief"] - b["actual_win_rate"]
        lines.append(f"| {b['range']} | {b['n']} | {b['avg_belief']*100:.1f}% | "
                     f"{b['actual_win_rate']*100:.1f}% | {bias*100:+.1f}pp |")

    lines += ["", "## 资金曲线（按结算时间）", "", "| 日期 | 资金 | 单笔 P&L | dir | edge |",
              "|---|---|---|---|---|"]
    for row in p["pnl_series"]:
        if row["t"] == "start":
            lines.append(f"| start | ${row['capital']:,.2f} | - | - | - |")
            continue
        lines.append(f"| {row['t']} | ${row['capital']:,.2f} | "
                     f"${row['pnl']:+,.2f} | {row.get('direction','-')} | "
                     f"{row.get('edge', 0)*100:+.1f}% |")

    return "\n".join(lines)
