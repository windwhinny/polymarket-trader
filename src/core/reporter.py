"""Report generation — P&L summaries and statistics."""
import json
import logging
from pathlib import Path
from typing import Optional
import numpy as np

from .types import Bet, MonthlyReport, BacktestResult

log = logging.getLogger("pm-backtest.report")


def generate_monthly_report(
    month: str,
    bets: list[Bet],
    starting_capital: float,
) -> MonthlyReport:
    """Generate a monthly P&L report from settled bets."""
    settled = [b for b in bets if b.pnl is not None]
    won = [b for b in settled if b.pnl > 0]
    lost = [b for b in settled if b.pnl < 0]
    unresolved = [b for b in bets if b.pnl is None]

    total_pnl = sum(b.pnl for b in settled)
    total_bet_amount = sum(b.amount for b in bets)
    ending_capital = starting_capital + total_pnl
    roi = total_pnl / starting_capital if starting_capital > 0 else 0
    win_rate = len(won) / len(settled) if settled else 0

    report = MonthlyReport(
        month=month,
        total_bets=len(bets),
        won=len(won),
        lost=len(lost),
        unresolved=len(unresolved),
        win_rate=win_rate,
        total_bet_amount=total_bet_amount,
        total_pnl=total_pnl,
        starting_capital=starting_capital,
        ending_capital=ending_capital,
        roi=roi,
        bets=bets,
    )

    log.info("REPORT | %s | bets=%d won=%d lost=%d unresolved=%d win_rate=%.1f%% pnl=%.2f roi=%.1f%%",
             month, report.total_bets, report.won, report.lost, report.unresolved,
             win_rate * 100, total_pnl, roi * 100)

    return report


def generate_final_report(
    monthly_reports: list[MonthlyReport],
    initial_capital: float,
) -> BacktestResult:
    """Generate the overall backtest report."""
    all_bets = []
    for r in monthly_reports:
        all_bets.extend(r.bets)

    settled = [b for b in all_bets if b.pnl is not None]
    total_pnl = sum(b.pnl for b in settled)
    total_roi = total_pnl / initial_capital if initial_capital > 0 else 0

    # Monthly P&L series for Sharpe ratio
    monthly_pnls = [r.total_pnl for r in monthly_reports]
    if len(monthly_pnls) > 1 and np.std(monthly_pnls) > 0:
        sharpe = (np.mean(monthly_pnls) / np.std(monthly_pnls)) * np.sqrt(12)
    else:
        sharpe = 0.0

    # Max drawdown
    capital_series = [initial_capital]
    for pnl in monthly_pnls:
        capital_series.append(capital_series[-1] + pnl)
    peak = capital_series[0]
    max_dd = 0.0
    for c in capital_series:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    won = [b for b in settled if b.pnl > 0]
    overall_win_rate = len(won) / len(settled) if settled else 0

    result = BacktestResult(
        months=monthly_reports,
        total_pnl=total_pnl,
        total_roi=total_roi,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        total_bets=len(all_bets),
        overall_win_rate=overall_win_rate,
    )

    return result


def save_report(result: BacktestResult, output_dir: str):
    """Save backtest results to JSON and print summary."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Save detailed JSON
    from .types import DataclassEncoder
    with open(out / "backtest_result.json", "w") as f:
        json.dump(result, f, cls=DataclassEncoder, indent=2, ensure_ascii=False)

    # Print summary
    print("\n" + "=" * 60)
    print(" POLYMARKET BACKTEST RESULTS")
    print("=" * 60)
    print(f" Period: 2025-01 ~ 2025-12")
    print(f" Initial Capital: ${result.months[0].starting_capital:,.2f}" if result.months else "")
    print(f" Final Capital:   ${result.months[-1].ending_capital:,.2f}" if result.months else "")
    print(f" Total P&L:       ${result.total_pnl:,.2f}")
    print(f" Total ROI:       {result.total_roi:.1%}")
    print(f" Sharpe Ratio:    {result.sharpe_ratio:.2f}")
    print(f" Max Drawdown:    {result.max_drawdown:.1%}")
    print(f" Total Bets:      {result.total_bets}")
    print(f" Win Rate:        {result.overall_win_rate:.1%}")
    print("-" * 60)
    print(f"{'Month':<10} {'Bets':>5} {'Won':>5} {'Lost':>5} {'Win%':>7} {'P&L':>10} {'ROI':>8} {'Capital':>10}")
    print("-" * 60)
    for r in result.months:
        print(f"{r.month:<10} {r.total_bets:>5} {r.won:>5} {r.lost:>5} "
              f"{r.win_rate:>6.1%} {r.total_pnl:>10.2f} {r.roi:>7.1%} {r.ending_capital:>10.2f}")
    print("=" * 60)

    log.info("Report saved to %s", out / "backtest_result.json")
