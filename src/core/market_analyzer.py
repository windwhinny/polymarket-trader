"""Market analyzer — orchestrates a deep-research investigation.

Pipeline (per market):
  1. plan_research: emit 2-4 research tasks covering different angles
     (typically for_yes / for_no / base_rate). Each is run as an independent
     research sub-agent in parallel; their search results land in a shared
     EvidenceStore.
  2. synthesize: planner reads each angle's evidence_ids, cluster-independent
     count, and assessment. It calls submit_analysis with:
       - model_prob, confidence, reasoning (free-text narrative)
       - claims: list of atomic factual statements, each citing evidence_ids
     The submission is mechanically validated: every citation must point at an
     evidence_id that actually exists in the store. Unknown ids → rejection.
  3. critic: blind review of (reasoning, model_prob, claims) only — does NOT
     see the underlying search results. Acts as a logical-consistency check.
  4. optionally apply critic's suggested action to the final verdict.

Trace layout (decisions/{date}/traces/{slug}/):
    ├── analyzer.json        — this orchestrator's own trace
    ├── research-1-{stance}.json
    ├── research-2-{stance}.json
    ├── research-3-{stance}.json
    ├── critic.json
    ├── verdict.json         — structured aggregate (consumed by parent runner)
    ├── sources.jsonl        — append-only source registry (de-duplicated)
    ├── evidence.jsonl       — append-only evidence store
    ├── claims.jsonl         — analyzer claims with verified citations
    └── ledger_summary.json  — counts (n_sources / n_clusters / n_evidence)
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .llm import LLMConfig
from .subagent import run_subagent
from .search_backend import SearchFn
from .research_agent import run_research
from .critic_agent import run_critic, apply_critic_action
from .evidence_store import EvidenceStore

log = logging.getLogger("pm-trader.analyzer")


PROMPT = """你是一个预测市场分析师。当前时间: {now}（你只能看到此时刻之前的信息）。

【市场】
slug: {slug}
问题: {question}
类别: {category}
YES 价格: {yes_price}（市场认为概率 {yes_pct}）
NO 价格: {no_price}
成交量: {volume}
结算日期: {end_date}（距今 {days_to_end} 天）
{journal_block}
【工作流程】
你不直接搜索。你的工作是：
1. 调用 plan_research 列出 2-4 个研究方向，每个方向交给一个独立的研究员调查。
   研究方向必须涵盖：
     - 至少一个收集"YES 会发生"证据的方向 (stance="for_yes")
     - 至少一个收集"NO/不会发生"证据的方向 (stance="for_no")
   视情况追加：基线方向 (stance="base_rate") 或中立调研 (stance="neutral")
   每个 angle 文字要具体（如"伊朗政府目前的稳定性指标"，不是"研究伊朗"）。
   研究员会自己用英文搜索、汇总成中文 assessment 给你；你不必在 angle 里强调语言。
   研究员会并行执行，结果汇总给你（含 evidence_ids 和 cluster-independent 计数）。

2. 看到结果后调用 submit_analysis 给出最终结论。
   每个 claim 必须 cite 真实的 evidence_id。编造的 id 会被拒绝。

【语言】
- reasoning 和 claims 的 statement 用中文（给中文用户看）
- 但 claims 引用的 evidence 来源大多是英文，你需要在中文 statement 中如实转述其内容

【submit_analysis 参数】
- model_prob: 综合各方向证据后，你估计的 YES 真实概率（0-1）
- confidence: high / medium / low / skip
- reasoning: 3-5 句**中文**总结判断
- claims: 关键事实声明数组，每条 {{ "statement": "中文事实陈述", "evidence_ids": ["E1","E5"] }}
  - 至少 1 条 claim
  - 每条 claim 必须有支持的 evidence_id（除非是显而易见的逻辑推断）

【判断规则】
- 模糊证据 + 高 confidence = 危险信号，老老实实降档
- pro 和 con 的 cluster-independent 计数相近 → confidence=skip
- 只有 weak 证据支持偏离 → confidence=low
- model_prob 与市场价偏差 < 3pp → confidence=skip
- 偏好 T-30d 以内的市场

【硬规则】
- 必须先 plan_research，不允许直接 submit_analysis
- 必须给出 claims（除非 confidence=skip 且无值得记录的事实）"""


def _build_planner_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "plan_research",
                "description": (
                    "派发 2-4 个研究方向。每个方向交给一个独立研究员并行调查。"
                    "调用后系统会自动执行所有方向并把结果（含 evidence_ids 和 cluster 计数）作为 tool_result 返回给你。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "angle": {
                                        "type": "string",
                                        "description": "具体研究方向，1-2 句话",
                                    },
                                    "stance": {
                                        "type": "string",
                                        "enum": ["for_yes", "for_no", "base_rate", "neutral"],
                                    },
                                },
                                "required": ["angle", "stance"],
                            },
                        }
                    },
                    "required": ["tasks"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_analysis",
                "description": "提交最终判断。仅在看到 plan_research 结果后调用。每个 claim 必须 cite 真实的 evidence_id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "model_prob": {"type": "number"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low", "skip"],
                        },
                        "reasoning": {"type": "string"},
                        "claims": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "statement": {"type": "string"},
                                    "evidence_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "stance": {
                                        "type": "string",
                                        "enum": ["for_yes", "for_no", "base_rate", "neutral"],
                                    },
                                },
                                "required": ["statement", "evidence_ids"],
                            },
                        },
                    },
                    "required": ["model_prob", "confidence", "reasoning", "claims"],
                },
            },
        },
    ]


def analyze_market(
    market: dict,
    llm_cfg: LLMConfig,
    search_fn: SearchFn,
    *,
    now_iso: str,
    cutoff_iso: Optional[str] = None,
    max_turns: int = 4,
    out_dir: Optional[Path] = None,
    research_parallel: int = 3,
    enable_critic: bool = True,
    journal: Optional[str] = None,
) -> dict:
    """Run the deep-research analyzer for a single market.

    Args:
      out_dir: directory to write all sub-traces + the per-market evidence
               ledger. One subdir per market.
      research_parallel: how many research sub-agents run concurrently per
               market (typically 3, the planner emits 2-4 tasks).
      enable_critic: if False, skip the critic stage (faster, less robust).
      journal: optional pre-rendered "trading journal" string with recent
               settled bets, surfaced to the agent so it can learn from past
               calls. Pass None to disable (cleanroom mode).
    """
    if out_dir is None:
        raise ValueError("out_dir is required for deep-research analyzer")
    out_dir.mkdir(parents=True, exist_ok=True)

    yes_price = float(market["yes_price"])
    no_price = float(market.get("no_price", 1 - yes_price))

    # One evidence ledger per market — sources/evidence/claims live here.
    store = EvidenceStore(out_dir)

    journal_block = ""
    if journal:
        journal_block = f"\n【近期交易回顾（仅供学习，不要锚定）】\n{journal}\n"

    system_prompt = PROMPT.format(
        now=now_iso,
        slug=market.get("slug", ""),
        question=market.get("question", ""),
        category=market.get("category", "General"),
        yes_price=f"{yes_price:.4f}",
        yes_pct=f"{yes_price:.1%}",
        no_price=f"{no_price:.4f}",
        volume=f"${market.get('volume', 0):,.0f}",
        end_date=market.get("end_date", "?"),
        days_to_end=market.get("days_to_end", "?"),
        journal_block=journal_block,
    )

    research_results: list[dict] = []

    # ── tool: plan_research ────────────────────────────────────────────
    def plan_research_handler(args, ctx):
        tasks = args.get("tasks", []) or []
        if not tasks:
            return json.dumps({"error": "tasks 不能为空"}, ensure_ascii=False)
        tasks = tasks[:4]
        log.info("[analyzer:%s] dispatching %d research tasks",
                 market.get("slug", "?")[:30], len(tasks))

        def run_one(idx_task):
            idx, t = idx_task
            stance = (t.get("stance") or "neutral").lower()
            if stance not in ("for_yes", "for_no", "base_rate", "neutral"):
                stance = "neutral"
            file_name = f"research-{idx+1}-{stance}.json"
            return run_research(
                market=market,
                angle=t.get("angle", ""),
                stance=stance,
                search_fn=search_fn,
                llm_cfg=llm_cfg,
                out_dir=out_dir,
                cutoff_iso=cutoff_iso,
                file_name=file_name,
                evidence_store=store,
                max_turns=6,
            )

        with ThreadPoolExecutor(max_workers=research_parallel) as pool:
            results = list(pool.map(run_one, list(enumerate(tasks))))

        research_results.extend(results)

        summary = {
            "research_results": [
                {
                    "stance": r["stance"],
                    "angle": r["angle"],
                    "strength": r["strength"],
                    "strength_claimed": r.get("strength_claimed", r["strength"]),
                    "strength_capped_by_clusters": r.get("strength_capped_by_clusters", False),
                    "assessment": r["assessment"],
                    "caveats": r.get("caveats", ""),
                    "evidence_count": r.get("evidence_count", 0),
                    "cluster_independent_count": r.get("cluster_independent_count", 0),
                    "evidence_ids": r.get("evidence_ids", []),
                    "trace_file": r.get("trace_file", ""),
                }
                for r in results
            ],
            "ledger": store.evidence_summary(),
            "note": "evidence_ids 在 submit_analysis 的 claims 里引用必须使用上面列表中的真实 id。",
        }
        return json.dumps(summary, ensure_ascii=False)

    # ── tool: submit_analysis ─────────────────────────────────────────
    submit_state = {"attempts": 0}

    def submit_handler(args, ctx):
        submit_state["attempts"] += 1
        mp = args.get("model_prob")
        if isinstance(mp, (int, float)):
            mp = max(0.01, min(0.99, float(mp)))
        else:
            mp = None
        conf = (args.get("confidence") or "skip").lower()
        if conf not in ("high", "medium", "low", "skip"):
            conf = "skip"

        # Validate claims: every supporting_evidence_id must exist
        raw_claims = args.get("claims", []) or []
        validated_claims = []
        any_invalid = False
        invalid_ids: list[str] = []
        for c in raw_claims:
            stmt = (c.get("statement") or "").strip()
            ids = c.get("evidence_ids", []) or []
            stance = (c.get("stance") or "neutral").lower()
            if not stmt:
                continue
            cid, missing = store.add_claim(
                statement=stmt,
                supporting_evidence_ids=ids,
                stance=stance,
                made_by="analyzer",
            )
            if missing:
                any_invalid = True
                invalid_ids.extend(missing)
            validated_claims.append({
                "claim_id": cid,
                "statement": stmt,
                "evidence_ids": ids,
                "missing": missing,
                "stance": stance,
            })

        # If confidence isn't skip but no claims given, that's a bad submission
        # (we want auditable claims for any non-skip verdict).
        no_claims = (conf != "skip" and not validated_claims)

        # Allow one rejection-and-retry round on invalid ids
        if (any_invalid or no_claims) and submit_state["attempts"] == 1:
            err = {
                "status": "rejected",
                "reason": ("missing claims" if no_claims else "invalid evidence_ids"),
                "invalid_evidence_ids": invalid_ids,
                "instruction": (
                    "请重新调用 submit_analysis。每条 claim 必须 cite 一个或多个真实 "
                    "evidence_id。如果 confidence != skip，至少给 1 条 claim。"
                ),
            }
            # NOTE: returning a dict-only result keeps the run alive; do not
            # finalize. Caller will see this as a tool_result and retry.
            return json.dumps(err, ensure_ascii=False)

        result = {
            "model_prob": mp,
            "confidence": conf,
            "reasoning": args.get("reasoning", ""),
            "claims": validated_claims,
            "claim_validation": {
                "had_invalid_ids": any_invalid,
                "had_missing_claims": no_claims,
                "attempts": submit_state["attempts"],
            },
        }
        return json.dumps({"status": "recorded"}, ensure_ascii=False), result

    planner_run = run_subagent(
        role="analyzer",
        system_prompt=system_prompt,
        tools=_build_planner_tools(),
        tool_handlers={
            "plan_research": plan_research_handler,
            "submit_analysis": submit_handler,
        },
        finalize_tool="submit_analysis",
        llm_cfg=llm_cfg,
        out_dir=out_dir,
        max_turns=max_turns,
        file_name="analyzer.json",
    )

    if planner_run.finalized:
        verdict = planner_run.result
    else:
        log.warning("[analyzer:%s] did not finalize (max_turns/error)",
                    market.get("slug", "?")[:30])
        verdict = {
            "model_prob": None,
            "confidence": "skip",
            "reasoning": "(planner did not finalize)",
            "claims": [],
        }

    # Direction is computed from model_prob vs yes_price; caller may also
    # recompute via Kelly path.
    mp = verdict.get("model_prob")
    if mp is None or verdict["confidence"] == "skip":
        direction = "SKIP"
    elif mp > yes_price:
        direction = "YES"
    elif mp < yes_price:
        direction = "NO"
    else:
        direction = "SKIP"
    verdict["direction"] = direction

    # ── critic ────────────────────────────────────────────────────────
    if enable_critic and verdict["confidence"] != "skip" and mp is not None:
        critic_result = run_critic(
            market=market,
            analyzer_result=verdict,
            llm_cfg=llm_cfg,
            out_dir=out_dir,
            file_name="critic.json",
        )
        verdict["critic"] = critic_result
        verdict = apply_critic_action(verdict, critic_result)
        if verdict["confidence"] == "skip":
            verdict["direction"] = "SKIP"
    else:
        verdict["critic"] = None

    # ── final aggregated result ───────────────────────────────────────
    verdict["slug"] = market.get("slug", "")
    verdict["question"] = market.get("question", "")
    verdict["yes_price"] = yes_price
    verdict["no_price"] = no_price
    verdict["volume"] = market.get("volume", 0)
    verdict["end_date"] = market.get("end_date", "")
    verdict["category"] = market.get("category", "")
    verdict["research"] = research_results
    verdict["analyzer_trace_file"] = "analyzer.json"
    verdict["search_queries"] = sorted({q for r in research_results for q in r.get("search_queries", [])})
    verdict["evidence_summary"] = store.evidence_summary()

    store.write_summary()

    with open(out_dir / "verdict.json", "w") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2, default=str)

    return verdict
