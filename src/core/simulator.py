"""Simulated trading engine with realistic fees."""
import logging
from typing import Optional

from .types import Bet, Market

log = logging.getLogger("pm-backtest.simulate")

TAKER_FEE_RATE = 0.0001   # 0.01% taker fee (2024-2025 Polymarket rate)
GAS_COST = 0.005          # $0.005 gas per trade (Polygon, negligible)
SPREAD_COST_RATE = 0.010  # 1% effective spread cost (half of ~2% bid-ask)


def simulate_bet(
    market: Market,
    month: str,
    direction: str,
    model_prob: float,
    market_prob: float,
    edge: float,
    kelly_fraction: float,
    capital: float,
) -> Bet:
    """Simulate placing a bet, accounting for all trading costs."""
    log.info("BET | %s | dir=%s capital=%.2f kelly=%.4f model=%.4f market=%.4f",
             market.slug, direction, capital, kelly_fraction, model_prob, market_prob)

    amount = capital * kelly_fraction

    if direction == "YES":
        entry_price = market_prob * (1 + SPREAD_COST_RATE)
    else:
        entry_price = (1 - market_prob) * (1 + SPREAD_COST_RATE)

    entry_price = max(0.01, min(0.99, entry_price))

    taker_fee = amount * TAKER_FEE_RATE
    total_cost = amount + taker_fee + GAS_COST

    log.debug("BET DETAIL | amount=%.4f entry=%.4f taker_fee=%.6f gas=%.4f total=%.4f shares=%.4f",
              amount, entry_price, taker_fee, GAS_COST, total_cost, amount / entry_price if entry_price else 0)

    bet = Bet(
        market_id=market.id,
        month=month,
        direction=direction,
        model_prob=model_prob,
        market_prob=market_prob,
        edge=edge,
        kelly_fraction=kelly_fraction,
        amount=amount,
        entry_price=entry_price,
    )
    return bet


def settle_bet(bet: Bet, market: Market) -> Bet:
    """Settle a bet based on market resolution."""
    if market.resolution is None:
        bet.resolution = "UNRESOLVED"
        bet.pnl = 0.0
        log.warning("SETTLE | %s | UNRESOLVED", market.id[:12])
        return bet

    bet.resolution = market.resolution

    taker_fee = bet.amount * TAKER_FEE_RATE

    if bet.direction == market.resolution:
        # Won
        shares = bet.amount / bet.entry_price if bet.entry_price > 0 else 0
        gross_return = shares * 1.0
        bet.pnl = gross_return - bet.amount - taker_fee - GAS_COST
        log.info("SETTLE | WON | dir=%s shares=%.2f gross=%.4f pnl=%.4f (%.1f%%)",
                 bet.direction, shares, gross_return, bet.pnl,
                 (bet.pnl / bet.amount * 100) if bet.amount else 0)
    else:
        bet.pnl = -(bet.amount + taker_fee + GAS_COST)
        log.info("SETTLE | LOST | dir=%s pnl=%.4f", bet.direction, bet.pnl)

    return bet
