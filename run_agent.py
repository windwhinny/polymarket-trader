#!/usr/bin/env python3
"""Legacy entry point — use trader.py backtest instead.

  python trader.py backtest --model deepseek-chat --start 2026-01 --end 2026-04
"""

import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.core.logger import setup_logger
from src.core.config import load_config
from src.core.llm import LLMConfig
from src.backtest.runner import run_backtest
from src.core.tracer import create_run_id
from src.core.reporter import save_report


def parse_args():
    p = argparse.ArgumentParser(description="Polymarket Agent Backtest (legacy)")
    p.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    p.add_argument("--model", default="deepseek-v4-pro")
    p.add_argument("--api-key", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--capital", type=float, default=0)
    p.add_argument("--min-volume", type=float, default=0)
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--run-id", default="")
    p.add_argument("--output", default="")
    return p.parse_args()


def main():
    args = parse_args()
    log = setup_logger("pm-backtest")
    config = load_config()

    if args.start: config["backtest"]["start_month"] = args.start
    if args.end: config["backtest"]["end_month"] = args.end
    if args.capital > 0: config["backtest"]["initial_capital"] = args.capital
    if args.min_volume > 0: config["backtest"]["min_monthly_volume"] = args.min_volume

    api_keys = config.get("api_keys", {})
    provider_keys = api_keys.get("deepseek", {})
    api_key = args.api_key or (os.environ.get("ANTHROPIC_API_KEY", "") if args.provider == "anthropic" else provider_keys.get("key", ""))
    base_url = args.base_url or ("" if args.provider == "anthropic" else provider_keys.get("base_url", ""))

    llm_cfg = LLMConfig(provider=args.provider, api_key=api_key, model=args.model, base_url=base_url)
    run_dir = args.output or f"runs/{create_run_id(args.run_id or llm_cfg.model.replace('/', '-'))}"
    log.info("Run dir: %s", run_dir)

    result = run_backtest(config, llm_cfg, run_dir, parallel=args.parallel)
    save_report(result, run_dir)
    return result


if __name__ == "__main__":
    main()
