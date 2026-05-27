"""Shared per-decision report writer.

Produces the same recommendations.md / predictions.json artifact for both
predict and backtest modes, so a backtest decision day can be debugged with
the same eyeballs as a real-time prediction.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional


log = logging.getLogger("pm-trader.report")


def write_decision_report(
    decisions: list[dict],
    capital: float,
    out_dir: Path,
    *,
    title: str,
    generated_at: str,
    model_label: str,
    extra_metadata: Optional[dict] = None,
) -> tuple[Path, Path]:
    """Render decisions list to recommendations.md and predictions.json.

    Each decision dict should have at least:
      slug, question (optional), yes_price, no_price (optional), volume (optional),
      end_date (optional), direction (YES/NO/SKIP), amount, confidence,
      reasoning, model_prob (optional), market_prob (optional), edge (optional)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    bets = [r for r in decisions if r.get("direction") not in (None, "SKIP")]
    skips = [r for r in decisions if r.get("direction") in (None, "SKIP")]
    total_bet = sum(r.get("amount", 0) for r in bets)

    lines = [
        f"# {title}",
        f"",
        f"**生成时间**: {generated_at}",
        f"**模型**: {model_label}",
        f"**总资金**: ${capital:,.2f}",
        f"**分析市场**: {len(decisions)} 个",
        f"**建议下注**: {len(bets)} 笔 | **跳过**: {len(skips)} 个",
        f"**总下注金额**: ${total_bet:,.2f} (仓位 {total_bet/capital*100:.1f}%)",
    ]
    if extra_metadata:
        for k, v in extra_metadata.items():
            lines.append(f"**{k}**: {v}")
    lines += ["", "---", "", "## 下注建议 (按金额排序)", ""]

    if bets:
        lines.append("| # | 方向 | 金额 | 置信度 | model_prob | 市场YES | edge | 市场 | 理由 |")
        lines.append("|---|------|------|--------|-----------|--------|------|------|------|")
        for i, b in enumerate(bets, 1):
            mp = b.get("model_prob")
            mkt = b.get("market_prob") or b.get("yes_price")
            edge = b.get("edge")
            mp_s = f"{mp:.1%}" if isinstance(mp, (int, float)) else "-"
            mkt_s = f"{mkt:.1%}" if isinstance(mkt, (int, float)) else "-"
            edge_s = f"{edge:+.1%}" if isinstance(edge, (int, float)) else "-"
            lines.append(
                f"| {i} | **{b['direction']}** | ${b.get('amount',0):.2f} | "
                f"{b.get('confidence','-')} | {mp_s} | {mkt_s} | {edge_s} | "
                f"{b['slug']} | {b.get('reasoning','')} |"
            )
    else:
        lines.append("*(无下注建议)*")

    lines += ["", "## 跳过的市场", ""]
    if skips:
        for s in skips:
            yp = s.get("yes_price")
            yp_s = f"{yp:.1%}" if isinstance(yp, (int, float)) else "?"
            lines.append(f"- [{s['slug']}] ({yp_s}) — {s.get('reasoning','')}")
    else:
        lines.append("*(全部市场均有下注建议)*")

    lines += [
        "",
        "---",
        "",
        "## 风险提示",
        "- AI 模型分析结果，不构成投资建议",
        "- 预测市场存在本金全部损失的风险",
    ]

    md_path = out_dir / "recommendations.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    json_path = out_dir / "predictions.json"
    payload = {
        "generated_at": generated_at,
        "model": model_label,
        "capital": capital,
        "total_markets": len(decisions),
        "bets": bets,
        "skips": [{"slug": s["slug"], "reasoning": s.get("reasoning", "")} for s in skips],
    }
    if extra_metadata:
        payload["metadata"] = extra_metadata
    with open(json_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log.info("Report: %s (%d bets, %d skips)", md_path, len(bets), len(skips))
    return md_path, json_path
