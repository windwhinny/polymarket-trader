"""Sub-agent invocation primitive.

A SubAgentRunner is the harness for one tool-calling LLM session: it owns the
message thread, calls the model, dispatches tools, persists a self-contained
trace, and returns a structured result. Used for analyzer, research,
critic — anything that's "give a model some tools and a goal, run until it
emits a finalize call".

Design goals:
  - Each sub-agent run is independently auditable: one JSON file with system
    prompt, every model turn, every tool call + result, and the final output.
  - Tool handlers can themselves spawn sub-agents (research → could call
    deeper research). We expose `subagent_dir` to handlers so they can place
    nested trace files under the parent.
  - No global state — all dependencies (LLM client, output dir) are injected.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .llm import LLMClient, LLMConfig

log = logging.getLogger("pm-trader.subagent")


# A tool handler receives (args_dict, ctx) and returns either a string (sent
# back as the tool result) or a tuple (str_result, finalize_value). When the
# second tuple element is non-None, the run terminates and that value becomes
# the SubAgentRun.result.
ToolHandler = Callable[[dict, "SubAgentCtx"], "str | tuple[str, Any]"]


class SubAgentCtx:
    """State passed into tool handlers — gives them access to where they can
    write nested traces and how to spawn deeper sub-agents.
    """
    __slots__ = ("subagent_dir", "llm_cfg", "shared")

    def __init__(self, subagent_dir: Path, llm_cfg: LLMConfig, shared: dict):
        self.subagent_dir = subagent_dir
        self.llm_cfg = llm_cfg
        self.shared = shared  # arbitrary per-decision data (e.g. market info)


class SubAgentRun:
    """Result of one sub-agent invocation."""
    def __init__(self, role: str, trace_path: Path, result: Any, finalized: bool):
        self.role = role
        self.trace_path = trace_path
        self.result = result
        self.finalized = finalized

    def as_reference(self) -> dict:
        """Compact pointer to include in parent trace."""
        return {
            "role": self.role,
            "trace_file": str(self.trace_path.name),
            "finalized": self.finalized,
        }


def run_subagent(
    *,
    role: str,
    system_prompt: str,
    tools: list[dict],
    tool_handlers: dict[str, ToolHandler],
    finalize_tool: str,
    llm_cfg: LLMConfig,
    out_dir: Path,
    shared: Optional[dict] = None,
    max_turns: int = 8,
    file_name: Optional[str] = None,
) -> SubAgentRun:
    """Run a tool-calling LLM until it calls `finalize_tool` (or hits max_turns).

    Args:
      role: short label (e.g. "research", "critic", "planner") — used in the
            trace filename and surfaces in logs.
      system_prompt: the system message for this sub-agent.
      tools: OpenAI-format tool definitions.
      tool_handlers: dict[tool_name -> handler]. The handler for `finalize_tool`
                    must return (str, value) where `value` becomes the run result.
      finalize_tool: which tool call ends the run.
      out_dir: where to write the per-run trace JSON. Created if missing.
      shared: passed through to handlers via SubAgentCtx.shared.
      max_turns: hard cap on (assistant turn) iterations.
      file_name: override the default {role}.json filename.

    Returns SubAgentRun. Even on max-turns / error, a trace file is written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / (file_name or f"{role}.json")

    client = LLMClient(llm_cfg)
    ctx = SubAgentCtx(subagent_dir=out_dir, llm_cfg=llm_cfg, shared=shared or {})

    trace: list[dict] = [{
        "role": "system",
        "ts": datetime.utcnow().isoformat(),
        "content": system_prompt,
    }]
    messages = [{"role": "system", "content": system_prompt}]

    finalized = False
    result: Any = None

    for turn in range(1, max_turns + 1):
        try:
            content, tool_calls, reasoning = client.chat(messages, tools)
        except Exception as e:
            log.error("[%s] LLM error turn %d: %s", role, turn, e)
            trace.append({"role": "error", "turn": turn, "error": str(e)})
            break

        if tool_calls:
            entry = {
                "role": "assistant",
                "turn": turn,
                "content": content or None,
                "tool_calls": [],
            }
            if reasoning:
                entry["reasoning_content"] = reasoning
            trace.append(entry)

            assistant_msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning

            tool_results = []  # collected to append after assistant message

            for tc in tool_calls:
                fn = tc["name"]
                args = tc.get("parsed_args") or {}
                if not args:
                    try:
                        args = json.loads(tc.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                entry["tool_calls"].append({"name": fn, "arguments": args})
                assistant_msg["tool_calls"].append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": fn, "arguments": tc.get("arguments", "{}")},
                })

                handler = tool_handlers.get(fn)
                if handler is None:
                    tool_result_str = json.dumps({"error": f"unknown tool: {fn}"})
                    finalize_value = None
                else:
                    try:
                        out = handler(args, ctx)
                    except Exception as e:
                        log.error("[%s] handler %s failed: %s", role, fn, e)
                        out = (json.dumps({"error": str(e)}), None)
                    if isinstance(out, tuple):
                        tool_result_str, finalize_value = out
                    else:
                        tool_result_str, finalize_value = out, None

                trace.append({
                    "role": "tool",
                    "name": fn,
                    "result": tool_result_str,
                })
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result_str,
                })

                if fn == finalize_tool and finalize_value is not None:
                    finalized = True
                    result = finalize_value

            messages.append(assistant_msg)
            messages.extend(tool_results)

            if finalized:
                break

        elif content:
            trace.append({"role": "assistant", "turn": turn, "content": content,
                          **({"reasoning_content": reasoning} if reasoning else {})})
            messages.append({"role": "assistant", "content": content})

    payload = {
        "role": role,
        "model": f"{llm_cfg.provider}/{llm_cfg.model}",
        "started_at": trace[0]["ts"],
        "finished_at": datetime.utcnow().isoformat(),
        "finalized": finalized,
        "result": result,
        "trace": trace,
    }
    with open(trace_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return SubAgentRun(role=role, trace_path=trace_path,
                       result=result, finalized=finalized)
