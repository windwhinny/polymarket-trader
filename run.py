"""Main backtest agent loop — orchestrates monthly backtesting."""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from src.logger import setup_logger
from src.config import load_config, Cache
from src.market_fetcher import fetch_markets
from src.price_fetcher import fetch_prices_at_month_end
from src.info_gatherer import search_context
from src.predictor import predict
from src.kelly import kelly_bet
from src.simulator import simulate_bet, settle_bet
from src.reporter import generate_monthly_report, generate_final_report, save_report
from src.types import Bet, Market, MonthlyReport, BacktestResult


def _month_end_date(year: int, month: int) -> str:
    if month == 12:
        return f"{year}-12-31"
    next_month = datetime(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    return last_day.strftime("%Y-%m-%d")


def _next_month(year: int, month: int) -> tuple[int, int]:
    month += 1
    if month > 12:
        month = 1
        year += 1
    return year, month


def run_month(
    year: int,
    month: int,
    capital: float,
    config: dict,
    cache: Cache,
    log: logging.Logger,
) -> tuple[list[Bet], float]:
    """Run backtest for a single month. Returns (bets, new_capital)."""
    month_key = f"{year}-{month:02d}"
    month_end_date = _month_end_date(year, month)
    cfg_kelly = config["kelly"]
    cfg_api = config["api"]

    log.info("=" * 60)
    log.info("MONTH %s | START | capital=%.2f", month_key, capital)
    log.info("=" * 60)

    markets = fetch_markets(
        year=year, month=month,
        min_volume=config["backtest"]["min_monthly_volume"],
        page_limit=cfg_api["page_limit"],
        max_pages=cfg_api.get("max_pages", 4),
        request_delay=cfg_api["request_delay"],
        cache=cache,
    )

    if not markets:
        log.warning("MONTH %s | NO MARKETS FOUND", month_key)
        return [], capital

    log.info("MONTH %s | %d qualified markets", month_key, len(markets))

    all_token_ids = []
    for m in markets:
        all_token_ids.extend(m.token_ids)
    prices = fetch_prices_at_month_end(
        all_token_ids, year, month,
        cache=cache, request_delay=cfg_api["request_delay"],
    )

    bets: list[Bet] = []
    current_capital = capital

    for i, market in enumerate(markets):
        log.info("[%d/%d] %s", i + 1, len(markets), market.slug)

        yes_token = market.token_ids[0] if len(market.token_ids) > 0 else ""
        market_prob = prices.get(yes_token)

        if market_prob is None or market_prob < 0.01 or market_prob > 0.99:
            log.debug("SKIP | %s | no valid price (%.4s)", market.slug, market_prob)
            continue

        ctx = search_context(
            query=market.question[:200],
            end_date=month_end_date,
            serpapi_api_key=config["api_keys"]["serpapi"]["key"],
            cache=cache,
        )

        pred = predict(
            market=market,
            market_prob=market_prob,
            search_ctx=ctx,
            api_key=config["api_keys"]["deepseek"]["key"],
            base_url=config["api_keys"]["deepseek"]["base_url"],
            model=config["api_keys"]["deepseek"]["model"],
            cache=cache,
        )

        direction, kelly_frac, edge = kelly_bet(
            model_prob=pred.model_prob,
            market_prob=market_prob,
            fraction=cfg_kelly["fraction"],
            min_edge=cfg_kelly["min_edge"],
            max_bet_pct=cfg_kelly["max_bet_pct"],
        )

        if direction == "SKIP":
            log.debug("SKIP | %s | edge=%.4f < min", market.slug, edge)
            continue

        bet = simulate_bet(
            market=market, month=month_key,
            direction=direction, model_prob=pred.model_prob,
            market_prob=market_prob, edge=edge,
            kelly_fraction=kelly_frac, capital=current_capital,
        )

        if bet.amount <= 0 or bet.amount > current_capital * 0.5:
            log.debug("SKIP | %s | bet amount %.2f unreasonable", market.slug, bet.amount)
            continue

        settle_bet(bet, market)
        bets.append(bet)

        if bet.pnl is not None:
            current_capital += bet.pnl

        log.info("CAPITAL | %d bets | %.2f", len(bets), current_capital)

        if current_capital < 10:
            log.warning("CAPITAL LOW | %.2f, stopping month", current_capital)
            break

    new_capital = current_capital
    log.info("MONTH %s | END | bets=%d pnl=%.2f capital %.2f→%.2f",
             month_key, len(bets), new_capital - capital, capital, new_capital)
    return bets, new_capital


def main():
    log = setup_logger("pm-backtest")
    log.info("=" * 60)
    log.info("POLYMARKET BACKTEST")
    log.info("=" * 60)

    config = load_config()
    cache_dir = Path(config["cache"]["dir"])
    if not cache_dir.is_absolute():
        cache_dir = Path(__file__).parent / cache_dir
    cache = Cache(str(cache_dir), config["cache"]["ttl_hours"])

    initial_capital = config["backtest"]["initial_capital"]
    start_year, start_month = map(int, config["backtest"]["start_month"].split("-"))
    end_year, end_month = map(int, config["backtest"]["end_month"].split("-"))

    log.info("CONFIG | capital=%.0f %d-%02d → %d-%02d vol>=%.0f kelly_frac=%.1f min_edge=%.2f",
             initial_capital, start_year, start_month, end_year, end_month,
             config["backtest"]["min_monthly_volume"],
             config["kelly"]["fraction"], config["kelly"]["min_edge"])

    monthly_reports: list[MonthlyReport] = []
    capital = initial_capital
    year, month = start_year, start_month

    while (year < end_year) or (year == end_year and month <= end_month):
        starting_capital = capital
        bets, capital = run_month(year, month, capital, config, cache, log)

        month_key = f"{year}-{month:02d}"
        report = MonthlyReport(
            month=month_key,
            total_bets=len(bets),
            won=len([b for b in bets if b.pnl is not None and b.pnl > 0]),
            lost=len([b for b in bets if b.pnl is not None and b.pnl < 0]),
            unresolved=len([b for b in bets if b.pnl is None]),
            win_rate=0.0,
            total_bet_amount=sum(b.amount for b in bets),
            total_pnl=capital - starting_capital,
            starting_capital=starting_capital,
            ending_capital=capital,
            roi=(capital - starting_capital) / starting_capital if starting_capital > 0 else 0,
            bets=bets,
        )
        settled = [b for b in bets if b.pnl is not None]
        report.win_rate = len([b for b in settled if b.pnl > 0]) / len(settled) if settled else 0
        monthly_reports.append(report)

        year, month = _next_month(year, month)

    result = generate_final_report(monthly_reports, initial_capital)
    save_report(result, str(Path(__file__).parent / "results"))
    return result


if __name__ == "__main__":
    main()
