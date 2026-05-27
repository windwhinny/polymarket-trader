#!/usr/bin/env python3
"""Polymarket Trader — AI-powered prediction market trading.

Usage:
  python trader.py backtest --model deepseek-chat --start 2026-01 --end 2026-04
  python trader.py backtest --provider anthropic --model claude-sonnet-4-20250514 --start 2026-01 --end 2026-04

Modes:
  backtest    Run historical backtest with autonomous agent
  trade       (future) Live trading on Polymarket
"""

import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def cmd_backtest(args):
    """Run a backtest with the autonomous agent."""
    from src.core.logger import setup_logger
    from src.core.config import load_config
    from src.core.llm import LLMConfig
    from src.backtest.runner import run_backtest
    from src.core.reporter import save_report

    log = setup_logger("pm-trader")
    config = load_config()

    # CLI overrides
    if args.start:
        config["backtest"]["start_month"] = args.start
    if args.end:
        config["backtest"]["end_month"] = args.end
    if args.capital:
        config["backtest"]["initial_capital"] = args.capital
    if args.min_volume:
        config["backtest"]["min_monthly_volume"] = args.min_volume

    # LLM config
    api_keys = config.get("api_keys", {})
    provider_keys = api_keys.get("deepseek", {})

    if args.provider == "anthropic":
        api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = ""
    else:
        api_key = args.api_key or provider_keys.get("key", "")
        base_url = args.base_url or provider_keys.get("base_url", "")

    llm_cfg = LLMConfig(provider=args.provider, api_key=api_key, model=args.model, base_url=base_url)

    from src.core.tracer import create_run_id
    run_dir = args.output or f"runs/{create_run_id(args.run_id or llm_cfg.model.replace('/', '-'))}"
    log.info("Run dir: %s", run_dir)

    result = run_backtest(config, llm_cfg, run_dir, parallel=args.parallel)
    save_report(result, run_dir)


def cmd_predict(args):
    """Run real-time market predictions."""
    from src.core.logger import setup_logger
    from src.core.config import load_config
    from src.core.llm import LLMConfig
    from src.predict.runner import run_predict
    from src.core.tracer import create_run_id

    log = setup_logger("pm-trader")
    config = load_config()

    api_keys = config.get("api_keys", {})
    provider_keys = api_keys.get("deepseek", {})

    if args.provider == "anthropic":
        api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = ""
    else:
        api_key = args.api_key or provider_keys.get("key", "")
        base_url = args.base_url or provider_keys.get("base_url", "")

    llm_cfg = LLMConfig(provider=args.provider, api_key=api_key, model=args.model, base_url=base_url)
    run_id = args.run_id or create_run_id("predict")
    output_dir = args.output or f"runs/{run_id}"
    log.info("Output: %s", output_dir)

    run_predict(config, llm_cfg, output_dir,
                capital=args.capital,
                min_volume=args.min_volume, parallel=args.parallel)


def main():
    p = argparse.ArgumentParser(description="Polymarket AI Trader")
    sub = p.add_subparsers(dest="mode", required=True)

    # backtest subcommand
    bt = sub.add_parser("backtest", help="Run historical backtest")
    bt.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    bt.add_argument("--model", default="deepseek-v4-pro")
    bt.add_argument("--api-key", default="")
    bt.add_argument("--base-url", default="")
    bt.add_argument("--start", default="")
    bt.add_argument("--end", default="")
    bt.add_argument("--capital", type=float, default=0)
    bt.add_argument("--min-volume", type=float, default=0)
    bt.add_argument("--parallel", type=int, default=4)
    bt.add_argument("--run-id", default="")
    bt.add_argument("--output", default="")

    # predict subcommand
    pr = sub.add_parser("predict", help="Real-time market predictions")
    pr.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    pr.add_argument("--model", default="deepseek-v4-pro")
    pr.add_argument("--api-key", default="")
    pr.add_argument("--base-url", default="")
    pr.add_argument("--capital", type=float, default=1000)
    pr.add_argument("--min-volume", type=float, default=10000)
    pr.add_argument("--parallel", type=int, default=5)
    pr.add_argument("--run-id", default="")
    pr.add_argument("--output", default="")

    # trade subcommand (future)
    tr = sub.add_parser("trade", help="Live trading (coming soon)")
    tr.add_argument("--dry-run", action="store_true", help="Simulation mode")

    args = p.parse_args()

    if args.mode == "backtest":
        cmd_backtest(args)
    elif args.mode == "predict":
        cmd_predict(args)
    elif args.mode == "trade":
        print("Live trading mode not implemented yet.")
        sys.exit(1)


if __name__ == "__main__":
    main()
