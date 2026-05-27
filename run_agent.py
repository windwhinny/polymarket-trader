#!/usr/bin/env python3
"""Polymarket Autonomous Agent Backtest — CLI Entry Point.

Examples:
  python run_agent.py --model deepseek-chat --start 2026-01 --end 2026-04
  python run_agent.py --provider anthropic --model claude-sonnet-4-20250514 \\
    --start 2026-01 --end 2026-04 --capital 5000
"""

import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.logger import setup_logger
from src.config import load_config
from src.llm import LLMConfig
from src.runner import run_backtest
from src.tracer import create_run_id
from src.reporter import save_report


def parse_args():
    p = argparse.ArgumentParser(description="Polymarket Agent Backtest")
    p.add_argument("--provider", default="openai", choices=["openai", "anthropic"],
                   help="LLM provider (default: openai)")
    p.add_argument("--model", default="deepseek-chat",
                   help="Model name (default: deepseek-chat)")
    p.add_argument("--api-key", default="",
                   help="Override API key (otherwise from config.yaml)")
    p.add_argument("--base-url", default="",
                   help="Override base URL (otherwise from config.yaml)")
    p.add_argument("--start", default="",
                   help="Start month YYYY-MM (override config)")
    p.add_argument("--end", default="",
                   help="End month YYYY-MM (override config)")
    p.add_argument("--capital", type=float, default=0,
                   help="Initial capital (override config)")
    p.add_argument("--min-volume", type=float, default=0,
                   help="Minimum market volume (override config)")
    p.add_argument("--parallel", type=int, default=4,
                   help="Parallel market fetchers (default: 4)")
    p.add_argument("--run-id", default="",
                   help="Custom run ID prefix (default: auto-generated)")
    p.add_argument("--output", default="",
                   help="Override output directory")
    return p.parse_args()


def main():
    args = parse_args()
    log = setup_logger("pm-backtest")

    config = load_config()

    # Apply CLI overrides
    if args.start:
        config["backtest"]["start_month"] = args.start
    if args.end:
        config["backtest"]["end_month"] = args.end
    if args.capital > 0:
        config["backtest"]["initial_capital"] = args.capital
    if args.min_volume > 0:
        config["backtest"]["min_monthly_volume"] = args.min_volume

    # Build LLM config (priority: CLI > config > env)
    api_keys = config.get("api_keys", {})
    provider_keys = api_keys.get("deepseek", {})  # Default deepseek key

    if args.provider == "anthropic":
        api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        base_url = ""
    else:
        api_key = args.api_key or provider_keys.get("key", "")
        base_url = args.base_url or provider_keys.get("base_url", "")

    llm_cfg = LLMConfig(
        provider=args.provider,
        api_key=api_key,
        model=args.model,
        base_url=base_url,
    )

    run_dir = args.output or f"runs/{create_run_id(args.run_id or llm_cfg.model.replace('/', '-'))}"
    log.info("Run dir: %s", run_dir)

    result = run_backtest(config, llm_cfg, run_dir, parallel=args.parallel)
    save_report(result, run_dir)
    return result


if __name__ == "__main__":
    main()
