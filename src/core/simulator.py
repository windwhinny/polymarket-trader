"""Simulated trading engine with realistic fees."""
import logging
from typing import Optional

from .types import Bet, Market

log = logging.getLogger("pm-backtest.simulate")

TAKER_FEE_RATE = 0.0001   # 0.01% taker fee (2024-2025 Polymarket rate)
GAS_COST = 0.005          # $0.005 gas per trade (Polygon, negligible)

# Effective half-spread paid per fill. Constant for now-at-the-money markets,
# scales up as the price moves toward 0 / 1 because the book thins out and the
# bid-ask widens.
BASE_SPREAD_RATE = 0.010   # 1% half-spread for prices near 0.5
TAIL_SPREAD_RATE = 0.025   # additional half-spread at the 0/1 rails


def effective_spread(price: float) -> float:
    """Spread half-cost as a multiplicative penalty on entry price.

    Linear in distance from 0.5: at price=0.5 returns BASE_SPREAD_RATE,
    at price=0 or price=1 returns BASE_SPREAD_RATE + TAIL_SPREAD_RATE.
    """
    p = max(0.0, min(1.0, float(price)))
    distance = abs(p - 0.5) * 2  # 0 at center, 1 at the rails
    return BASE_SPREAD_RATE + TAIL_SPREAD_RATE * distance


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

    raw_price = market_prob if direction == "YES" else (1 - market_prob)
    spread = effective_spread(raw_price)
    entry_price = raw_price * (1 + spread)
    entry_price = max(0.01, min(0.99, entry_price))

    taker_fee = amount * TAKER_FEE_RATE
    total_cost = amount + taker_fee + GAS_COST

    log.debug("BET DETAIL | amount=%.4f raw_price=%.4f spread=%.4f entry=%.4f taker_fee=%.6f gas=%.4f total=%.4f shares=%.4f",
              amount, raw_price, spread, entry_price, taker_fee, GAS_COST,
              total_cost, amount / entry_price if entry_price else 0)

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
    """Settle a bet based on market resolution.

    Holding to expiry on Polymarket means the winning shares pay $1 each in the
    auto-redemption flow — there's no second taker fee or spread on payout, so
    only entry costs matter here.
    """
    if market.resolution is None:
        bet.resolution = "UNRESOLVED"
        bet.pnl = 0.0
        log.warning("SETTLE | %s | UNRESOLVED", market.id[:12])
        return bet

    bet.resolution = market.resolution

    taker_fee = bet.amount * TAKER_FEE_RATE

    if bet.direction == market.resolution:
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


def early_close_bet(bet: Bet, current_yes_price: float, exit_at: str) -> Bet:
    """Close a bet at the current market price (early exit, before resolution).

    Unlike settle_bet, this is a real second leg: pay another taker fee + half-
    spread to cross the book back out. The proceeds are
        shares × current_position_price × (1 - spread)
    where current_position_price is the YES price for a YES bet, (1-YES) for NO.
    """
    if bet.entry_price <= 0:
        bet.resolution = "EARLY_CLOSE"
        bet.pnl = 0.0
        return bet

    if bet.direction == "YES":
        position_price = float(current_yes_price)
    else:
        position_price = 1 - float(current_yes_price)
    position_price = max(0.01, min(0.99, position_price))

    shares = bet.amount / bet.entry_price
    spread = effective_spread(position_price)
    sale_price = position_price * (1 - spread)
    sale_price = max(0.0, min(1.0, sale_price))

    gross = shares * sale_price
    taker_fee = bet.amount * TAKER_FEE_RATE  # entry fee already baked into amount cost
    exit_fee = gross * TAKER_FEE_RATE
    bet.pnl = gross - bet.amount - taker_fee - exit_fee - GAS_COST
    bet.resolution = "EARLY_CLOSE"
    bet.settled_at = exit_at
    log.info("EARLY CLOSE | %s | dir=%s entry=%.3f current=%.3f sale=%.3f "
             "shares=%.2f gross=%.2f pnl=%.2f",
             bet.market_id[:12], bet.direction, bet.entry_price, position_price,
             sale_price, shares, gross, bet.pnl)
    return bet
