"""Multi-provider LLM client — OpenAI / Anthropic / DeepSeek compatible API."""
import json
import logging
from typing import Optional
from dataclasses import dataclass

log = logging.getLogger("pm-backtest.llm")


@dataclass
class LLMConfig:
    provider: str  # "openai" or "anthropic"
    api_key: str
    model: str
    base_url: str = ""


class LLMClient:
    """Unified chat completion interface across providers."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        if cfg.provider == "anthropic":
            import anthropic
            kwargs = {"api_key": cfg.api_key}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            self._client = anthropic.Anthropic(**kwargs)
        else:
            from openai import OpenAI
            url = cfg.base_url or "https://api.openai.com/v1"
            self._client = OpenAI(api_key=cfg.api_key, base_url=url)

    def chat(self, messages: list, tools: list):
        """Send chat request and return (content, tool_calls_list, reasoning_content)."""
        if self.cfg.provider == "anthropic":
            return self._chat_anthropic(messages, tools)
        else:
            return self._chat_openai(messages, tools)

    def _chat_openai(self, messages, tools):
        kwargs = {
            "model": self.cfg.model,
            "messages": messages,
            "timeout": 120,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        response = self._client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", "") or ""

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                    "parsed_args": args,
                })

        return content, tool_calls, reasoning

    def _chat_anthropic(self, messages, tools):
        system = ""
        anthropic_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            elif m["role"] == "tool":
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}]
                })
            elif m["role"] == "assistant" and m.get("tool_calls"):
                content_blocks = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]),
                    })
                anthropic_msgs.append({"role": "assistant", "content": content_blocks})
            else:
                content = m.get("content", "")
                if m["role"] == "assistant" and not content:
                    continue
                anthropic_msgs.append({"role": m["role"], "content": content})
        if not anthropic_msgs:
            anthropic_msgs.append({"role": "user", "content": "Begin."})

        if tools:
            anthropic_tools = []
            for t in tools:
                func = t["function"]
                params = func.get("parameters", {}) or {}
                props_in = params.get("properties", {}) or {}
                props_out = {}
                for k, v in props_in.items():
                    spec = dict(v) if isinstance(v, dict) else {"type": "string"}
                    spec.setdefault("type", "string")
                    props_out[k] = spec
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": {
                        "type": "object",
                        "properties": props_out,
                        "required": params.get("required", []),
                    }
                })
        else:
            anthropic_tools = None

        response = self._client.messages.create(
            model=self.cfg.model,
            max_tokens=4096,
            system=system,
            messages=anthropic_msgs,
            tools=anthropic_tools,
        )

        content = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                    "parsed_args": block.input,
                })

        return content, tool_calls, ""
