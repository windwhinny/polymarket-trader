"""Critic sub-agent — independent skeptic that reviews the analyzer's verdict.

Critic ONLY sees:
  - market question + price
  - analyzer's reasoning + model_prob + confidence

Critic does NOT see:
  - the underlying search evidence

Why blind: if both the analyzer and critic see the same evidence, they tend
to converge on the same narrative. By limiting the critic to the reasoning
output, it acts as a logical-consistency check: "given just what the analyzer
wrote, is this conclusion warranted? Are there obvious counter-stories that
the reasoning doesn't address?"
"""

import json
import logging
from pathlib import Path

from .llm import LLMConfig
from .subagent import run_subagent

log = logging.getLogger("pm-trader.critic")


PROMPT = """你是一个怀疑论质量审查员。一个分析师已经针对下面的市场提交了判断。
你的任务是**只看分析师的论证**，挑出逻辑漏洞、未考虑的反证、过度自信的迹象。
你看不到分析师查询过的原始搜索结果。

【市场】
slug: {slug}
问题: {question}
当前 YES 价格: {yes_pct}

【分析师结论】
- model_prob: {model_prob_pct}
- confidence: {confidence}
- 论证: {reasoning}

【审查标准】
1. 论证强度 vs confidence 是否匹配？模糊的措辞配 high confidence 是危险信号。
2. 论证是否考虑了反向情形？还是单方面陈述？
3. model_prob 与论证措辞一致吗？说"几乎肯定"配 65% 矛盾。
4. 时间窗口与结算日期一致吗？提到的事件在结算前发生吗？
5. 是否锚定到市场价（如 model_prob 仅与市场价偏差 5%）？

调用 finish_critic 提交结论。

【finish_critic 参数】
- approves: true (论证扎实) | false (有重要问题)
- concerns: 数组，每条一个具体问题。空数组表示无问题。
- suggested_action: "keep" (保持原判断) | "lower_confidence" (降一档) | "flip_to_skip" (改为不下注) | "flip_direction" (方向相反)

宁严不松。如果论证薄弱却 confidence=high，应该 lower_confidence。"""


def _build_critic_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "finish_critic",
                "description": "提交审查结论。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "approves": {"type": "boolean"},
                        "concerns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "suggested_action": {
                            "type": "string",
                            "enum": ["keep", "lower_confidence", "flip_to_skip", "flip_direction"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["approves", "concerns", "suggested_action"],
                },
            },
        },
    ]


def run_critic(
    *,
    market: dict,
    analyzer_result: dict,
    llm_cfg: LLMConfig,
    out_dir: Path,
    file_name: str = "critic.json",
    max_turns: int = 2,
) -> dict:
    """Returns critic verdict dict. Always includes trace_file."""
    yes_price = float(market["yes_price"])
    mp = analyzer_result.get("model_prob")
    if isinstance(mp, (int, float)):
        mp_str = f"{mp*100:.1f}%"
    else:
        mp_str = "(missing)"
    system_prompt = PROMPT.format(
        slug=market.get("slug", ""),
        question=market.get("question", ""),
        yes_pct=f"{yes_price:.1%}",
        model_prob_pct=mp_str,
        confidence=analyzer_result.get("confidence", "skip"),
        reasoning=analyzer_result.get("reasoning", ""),
    )

    def finish_handler(args, ctx):
        payload = {
            "approves": bool(args.get("approves", False)),
            "concerns": args.get("concerns", []),
            "suggested_action": args.get("suggested_action", "keep"),
            "rationale": args.get("rationale", ""),
        }
        return json.dumps({"status": "recorded"}, ensure_ascii=False), payload

    run = run_subagent(
        role="critic",
        system_prompt=system_prompt,
        tools=_build_critic_tools(),
        tool_handlers={"finish_critic": finish_handler},
        finalize_tool="finish_critic",
        llm_cfg=llm_cfg,
        out_dir=out_dir,
        max_turns=max_turns,
        file_name=file_name,
    )

    if not run.finalized:
        return {
            "approves": True,  # don't block on critic failure
            "concerns": [],
            "suggested_action": "keep",
            "rationale": "(critic did not finalize)",
            "trace_file": run.trace_path.name,
        }

    result = run.result
    result["trace_file"] = run.trace_path.name
    return result


def apply_critic_action(analyzer_result: dict, critic: dict) -> dict:
    """Apply suggested_action to produce a (possibly modified) verdict."""
    out = dict(analyzer_result)
    action = critic.get("suggested_action", "keep")
    if action == "keep" or critic.get("approves"):
        return out
    if action == "flip_to_skip":
        out["confidence"] = "skip"
        out["direction"] = "SKIP"
        out["reasoning"] = (out.get("reasoning", "") + "  [critic flipped to skip]").strip()
        return out
    if action == "lower_confidence":
        ladder = ["high", "medium", "low", "skip"]
        cur = out.get("confidence", "low")
        if cur in ladder:
            idx = ladder.index(cur)
            out["confidence"] = ladder[min(idx + 1, len(ladder) - 1)]
        else:
            out["confidence"] = "low"
        out["reasoning"] = (out.get("reasoning", "") + "  [critic lowered confidence]").strip()
        if out["confidence"] == "skip":
            out["direction"] = "SKIP"
        return out
    if action == "flip_direction":
        # Flipping direction without a flip in model_prob is suspicious — only
        # do it if the agent's model_prob is on the opposite side of market.
        # Otherwise, defang to skip.
        out["confidence"] = "skip"
        out["direction"] = "SKIP"
        out["reasoning"] = (out.get("reasoning", "") + "  [critic flagged direction]").strip()
        return out
    return out
