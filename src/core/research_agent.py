"""Research sub-agent — investigates one focused question deeply.

The analyzer dispatches several of these in parallel, each with a single angle
to investigate (e.g. "what evidence is FOR X happening?", "what's the base
rate?", "are there recent shocks against X?"). Each instance runs its own
search loop, returns evidence-backed claims plus a cluster-independent
strength assessment, and writes a self-contained trace.

Search results enter a shared EvidenceStore; each row carries a stable
evidence_id that the agent quotes when finalizing. This means:
  - downstream agents (analyzer, critic) can cite specific evidence by id;
  - the strength estimate uses cluster-independent counts (5 articles from
    one wire-syndication cluster == 1 independent source), not raw counts;
  - the audit trail is reproducible: claim → evidence_id → source_id → URL.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from .llm import LLMConfig
from .subagent import run_subagent
from .search_backend import SearchFn
from .evidence_store import EvidenceStore

log = logging.getLogger("pm-trader.research")


PROMPT = """你是一个深度研究员，针对一个预测市场的某个具体问题做调查。

【市场背景】
slug: {slug}
问题: {question}
当前 YES 价格: {yes_pct}（市场认为概率）
结算日期: {end_date}（距今 {days_to_end} 天）

【你的研究方向】
{angle}

立场：{stance}
（如果立场是 "for_yes"，你的任务是收集"YES 会发生"的证据；
  "for_no" 则收集"NO/不会发生"的证据；
  "base_rate" 收集类似事件的历史基线；
  "neutral" 客观调研。）

【语言策略 — 重要】
- search_news 的 query **必须用英文**（除非话题本身只有中文来源，例如中国国内政策细节）。
  英文来源覆盖更广、时效性更好、独立来源更多。
- 优先使用英文官方 / 主流媒体来源（Reuters / AP / NYT / FT / 政府公告 / 上市公司财报等）。
- 你的 assessment / caveats 输出**用中文**——这是给上层分析师看的总结。

【任务要求】
1. 调用 search_news 搜索 2-4 次（不同英文关键词组合，不要重复关键词）。
2. 每次搜索的结果都会带上 evidence_id（如 E12, E13）—— 你引用证据时必须使用这些 id。
3. 收齐证据后调用 finish_research 提交结构化结果。

【finish_research 参数】
- evidence_ids: 一个数组，列出你认定支持本立场的 evidence_id（例如 ["E12","E14"]）
  注意：只列**真实存在**于搜索结果中的 id；编造的 id 会让本次研究作废。
- assessment: 1-2 句**中文**结论
- strength: "strong" | "medium" | "weak"
  注意：strength 不应该看证据条数，而应该看**独立来源数**。同一家媒体的多篇报道
  通常算作一个独立来源；权威一手来源（如政府公告、企业财报）才算 strong。
- caveats: 重要的反向或限制条件（如有），**中文**

如果 4 次搜索后仍找不到证据，把 strength 标为 "weak"，evidence_ids 留空 []。"""


def _build_research_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_news",
                "description": "搜索相关新闻和信息。返回的 articles 每条带 evidence_id。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish_research",
                "description": "提交研究结果。evidence_ids 必须引用真实存在的 id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "证据 id 列表，如 ['E12','E14']",
                        },
                        "assessment": {"type": "string"},
                        "strength": {"type": "string", "enum": ["strong", "medium", "weak"]},
                        "caveats": {"type": "string"},
                    },
                    "required": ["evidence_ids", "assessment", "strength"],
                },
            },
        },
    ]


def run_research(
    *,
    market: dict,
    angle: str,
    stance: str,
    search_fn,
    llm_cfg: LLMConfig,
    out_dir: Path,
    cutoff_iso: Optional[str],
    file_name: str,
    evidence_store: EvidenceStore,
    max_turns: int = 6,
) -> dict:
    """Returns the research finalize payload (or a default-skip dict)."""
    yes_price = float(market["yes_price"])
    system_prompt = PROMPT.format(
        slug=market.get("slug", ""),
        question=market.get("question", ""),
        yes_pct=f"{yes_price:.1%}",
        end_date=market.get("end_date", "?"),
        days_to_end=market.get("days_to_end", "?"),
        angle=angle,
        stance=stance,
    )

    queries: list[str] = []
    seen_evidence_ids: list[str] = []  # all eids this research session encountered

    role_label = f"research-{stance}"

    def search_handler(args, ctx):
        q = args.get("query", "")
        queries.append(q)
        try:
            articles = search_fn(q, cutoff_iso) or []
        except Exception as e:
            log.warning("research search failed for %r: %s", q[:60], e)
            articles = []

        # Register each article as a source + evidence
        registered = []
        for a in articles[:5]:
            url = a.get("source") or ""
            title = a.get("title") or ""
            date = a.get("date") or ""
            snippet = a.get("snippet") or ""
            try:
                sid = evidence_store.add_source(url=url, title=title, date=date)
                eid = evidence_store.add_evidence(
                    source_id=sid,
                    claim=title or snippet[:120],
                    snippet=snippet,
                    stance=stance,
                    weight="medium",
                    contributing_research=role_label,
                )
                seen_evidence_ids.append(eid)
                registered.append({
                    "evidence_id": eid,
                    "source_id": sid,
                    "title": title,
                    "snippet": snippet,
                    "date": date,
                    "url": url,
                })
            except Exception as e:
                log.warning("evidence registration failed: %s", e)

        if registered:
            payload = {
                "status": "ok",
                "query": q,
                "result_count": len(registered),
                "articles": registered,
                "note": "引用证据时使用上面的 evidence_id（E*）。",
            }
        else:
            payload = {
                "status": "no_results",
                "query": q,
                "note": "搜索无结果。换关键词或基于已有信息。",
            }
        return json.dumps(payload, ensure_ascii=False)

    def finish_handler(args, ctx):
        ids = args.get("evidence_ids", []) or []
        # Validate: drop unknown ids and report them
        validated = []
        unknown = []
        for eid in ids:
            if evidence_store.get_evidence(eid) is None:
                unknown.append(eid)
            else:
                validated.append(eid)

        cluster_count = evidence_store.cluster_independent_count(validated)

        # Honest strength: cap at the cluster floor to discourage inflation.
        claimed_strength = (args.get("strength") or "weak").lower()
        if cluster_count == 0:
            adjusted_strength = "weak"
        elif cluster_count == 1:
            adjusted_strength = "weak" if claimed_strength == "strong" else claimed_strength
        elif cluster_count == 2:
            adjusted_strength = "medium" if claimed_strength == "strong" else claimed_strength
        else:
            adjusted_strength = claimed_strength

        payload = {
            "evidence_ids": validated,
            "unknown_evidence_ids_dropped": unknown,
            "cluster_independent_count": cluster_count,
            "evidence_count": len(validated),
            "assessment": args.get("assessment", ""),
            "strength_claimed": claimed_strength,
            "strength": adjusted_strength,
            "strength_capped_by_clusters": adjusted_strength != claimed_strength,
            "caveats": args.get("caveats", ""),
            "search_queries": list(queries),
            "stance": stance,
            "angle": angle,
            "all_seen_evidence_ids": list(seen_evidence_ids),
        }
        return json.dumps({"status": "recorded",
                            "validated": validated,
                            "unknown_dropped": unknown,
                            "cluster_count": cluster_count}, ensure_ascii=False), payload

    run = run_subagent(
        role=role_label,
        system_prompt=system_prompt,
        tools=_build_research_tools(),
        tool_handlers={
            "search_news": search_handler,
            "finish_research": finish_handler,
        },
        finalize_tool="finish_research",
        llm_cfg=llm_cfg,
        out_dir=out_dir,
        max_turns=max_turns,
        file_name=file_name,
    )

    if not run.finalized:
        log.warning("[research-%s] did not finalize (max_turns / error)", stance)
        return {
            "evidence_ids": [],
            "evidence_count": 0,
            "cluster_independent_count": 0,
            "assessment": "(超时/未完成)",
            "strength": "weak",
            "caveats": "",
            "search_queries": list(queries),
            "stance": stance,
            "angle": angle,
            "all_seen_evidence_ids": list(seen_evidence_ids),
            "trace_file": run.trace_path.name,
        }

    result = run.result or {}
    result["trace_file"] = run.trace_path.name
    return result
