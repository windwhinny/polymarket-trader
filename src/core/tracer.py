"""Trace recorder — saves full agent execution trace to JSONL for analysis."""
import json
import os
import time
from pathlib import Path
from datetime import datetime


class Tracer:
    """Records every step of an agent run to a JSONL file."""

    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_file = self.run_dir / "trace.jsonl"
        self.bets_file = self.run_dir / "bets.jsonl"
        self._f = None
        self._bf = None
        self.run_id = self.run_dir.name
        self.start_time = datetime.now().isoformat()

    @property
    def f(self):
        if self._f is None:
            self._f = open(self.trace_file, "w")
        return self._f

    @property
    def bf(self):
        if self._bf is None:
            self._bf = open(self.bets_file, "w")
        return self._bf

    def _write(self, data: dict):
        data["ts"] = datetime.now().isoformat()
        data["run_id"] = self.run_id
        self.f.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.f.flush()

    def system(self, prompt: str):
        self._write({"type": "system_prompt", "content": prompt})

    def turn_start(self, n: int):
        self._write({"type": "turn_start", "turn": n})

    def model_call(self, messages_count: int, provider: str, model: str, temperature: float):
        self._write({"type": "model_call", "messages_count": messages_count,
                     "provider": provider, "model": model, "temperature": temperature})

    def model_response(self, content: str, tool_calls: list):
        self._write({"type": "model_response", "content": content[:500],
                     "tool_calls": [{"name": tc["name"], "args": tc.get("parsed_args", tc.get("arguments", ""))}
                                    for tc in tool_calls]})

    def tool_call(self, name: str, args: dict):
        self._write({"type": "tool_call", "name": name, "args": args})

    def tool_result(self, name: str, result: str):
        self._write({"type": "tool_result", "name": name, "result": result[:500]})

    def bet(self, month: str, direction: str, amount: float, slug: str, pnl: float, resolution: str, reasoning: str):
        data = {
            "type": "bet",
            "month": month, "direction": direction, "amount": amount,
            "slug": slug, "pnl": pnl, "resolution": resolution,
            "reasoning": reasoning[:300],
        }
        self._write(data)
        self.bf.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.bf.flush()

    def finish(self, month: str, capital: float, summary: str, decisions: str):
        self._write({"type": "finish", "month": month, "final_capital": capital,
                     "summary": summary[:300], "decisions": decisions[:300]})

    def error(self, error: str):
        self._write({"type": "error", "error": error})

    def save_config(self, config: dict):
        with open(self.run_dir / "config.yaml", "w") as f:
            import yaml
            yaml.dump(config, f)

    def save_result(self, result: dict):
        with open(self.run_dir / "result.json", "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    def close(self):
        if self._f:
            self._f.close()
        if self._bf:
            self._bf.close()
        end_time = datetime.now().isoformat()
        manifest = {
            "run_id": self.run_id,
            "start_time": self.start_time,
            "end_time": end_time,
            "trace_file": str(self.trace_file),
            "bets_file": str(self.bets_file),
            "config_file": str(self.run_dir / "config.yaml"),
            "result_file": str(self.run_dir / "result.json"),
        }
        with open(self.run_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)


def create_run_id(prefix: str = "run") -> str:
    """Generate a unique run ID: run-2026-05-27-223000"""
    return f"{prefix}-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}"
