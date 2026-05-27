"""Screener agent — quickly filter a long list of markets to a few worth analyzing.

Returns a per-market verdict: selected=True / False, plus a one-line reason
that gets surfaced to the user (so "skipped" markets aren't opaque).
"""

import json
import logging
from typing import Optional

from .llm import LLMClient, LLMConfig

log = logging.getLogger("pm-trader.screener")


PROMPT = """你是一个预测市场筛选员。下面是 {n} 个候选市场，请快速浏览。

{market_list}

筛选目标：选出值得深入分析的 5-10 个市场（其余跳过）。
判断标准：
- YES 价格与你直觉/常识明显不符（≥5pp 偏差）
- 你有相关领域知识可以评估
- 优先 T-30d 以内的市场
- 跳过纯随机市场（如未知队伍比赛）和远期事件

【输出 JSON】（仅输出 JSON，不要 markdown）：
{{
  "verdicts": [
    {{"slug": "...", "selected": true,  "reason": "为什么值得分析"}},
    {{"slug": "...", "selected": false, "reason": "为什么跳过"}}
  ]
}}

每个 slug 都必须出现一次，selected=true 的总数控制在 5-10 个。"""


def screen_markets(
    markets: list[dict],
    llm_cfg: LLMConfig,
    *,
    max_select: int = 10,
) -> dict[str, dict]:
    """Returns {slug: {"selected": bool, "reason": str}} keyed by slug.

    On parse failure or LLM error, falls back to selecting the top
    `max_select` by volume with reason "(screener fallback)".
    """
    if len(markets) <= max_select:
        return {m["slug"]: {"selected": True, "reason": "(small list, all included)"}
                for m in markets}

    client = LLMClient(llm_cfg)

    lines = []
    for i, m in enumerate(markets):
        days = m.get("days_to_end")
        days_s = f"T-{days}d" if isinstance(days, int) else "T-?"
        lines.append(
            f"{i+1}. [{m['slug']}] YES={m['yes_price']:.1%} | vol=${m['volume']:,.0f} | {days_s} | {m['question']}"
        )
    prompt = PROMPT.format(n=len(markets), market_list="\n".join(lines))

    try:
        content, _, _ = client.chat([{"role": "user", "content": prompt}], [])
        parsed = json.loads(content) if isinstance(content, str) else content
        verdicts = parsed.get("verdicts", [])
        out: dict[str, dict] = {}
        for v in verdicts:
            slug = v.get("slug")
            if not slug:
                continue
            out[slug] = {
                "selected": bool(v.get("selected", False)),
                "reason": v.get("reason", ""),
            }
        # Fill any missing slugs with skip / no reason
        for m in markets:
            out.setdefault(m["slug"], {"selected": False, "reason": "(not surfaced by screener)"})

        # Cap selections at max_select; mark overflow as skipped.
        selected = [s for s, v in out.items() if v["selected"]]
        if len(selected) > max_select:
            for s in selected[max_select:]:
                out[s] = {"selected": False, "reason": f"(over screener cap of {max_select})"}

        log.info("Screen: %d → %d selected", len(markets), sum(1 for v in out.values() if v["selected"]))
        return out
    except Exception as e:
        log.error("Screen parse failed (%s); falling back to top-%d by volume", e, max_select)
        sorted_markets = sorted(markets, key=lambda m: -m.get("volume", 0))
        out = {}
        for i, m in enumerate(sorted_markets):
            sel = i < max_select
            out[m["slug"]] = {
                "selected": sel,
                "reason": "(screener fallback: top by volume)" if sel else "(screener fallback: out of top)",
            }
        return out
