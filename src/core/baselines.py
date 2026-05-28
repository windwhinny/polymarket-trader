"""Baseline strategies for backtest comparison.

Each baseline implements the same decision-day pipeline as the LLM analyzer,
but without LLM calls — so we can measure how much of the P&L is "edge from
the model" vs "edge from being in the market at all".

Available baselines:
  - always-skip:   never bet. P&L = 0. Sanity floor.
  - market-prob:   model_prob = market YES price → no edge → always SKIP.
                   Equivalent to always-skip in practice but proves the
                   MIN_EDGE filter is wired correctly.
  - anti-favorite: bet NO on every market with YES ≤ 0.10 (cheap longshot
                   premium hypothesis), confidence=low. Tests whether the
                   "longshot bias" exploit alone captures market premium.
  - random:        for each candidate market, pick YES/NO with 50/50 and
                   confidence=low. Pure noise; deviation from this floor is
                   what attribution should care about.

Each baseline returns a list of (decision_day, list[Bet], list[decision_dict])
that the runner consumes in place of the LLM pipeline.
"""

import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .types import Bet
from .kelly import params_from_config, size_bet
from .simulator import max_affordable_amount, simulate_bet

log = logging.getLogger("pm-trader.baseline")


# A baseline takes (market_dict) and returns a per-market verdict-style dict
# matching the analyzer's output shape, OR None to skip.
BaselineFn = Callable[[dict], dict]


def baseline_always_skip(market: dict) -> dict:
    return {
        "model_prob": market["yes_price"],
        "confidence": "skip",
        "reasoning": "[baseline:always-skip] never bets",
        "claims": [],
    }


def baseline_market_prob(market: dict) -> dict:
    yes = market["yes_price"]
    return {
        "model_prob": yes,
        "confidence": "skip",
        "reasoning": "[baseline:market-prob] mirrors market price → zero edge → SKIP",
        "claims": [],
    }


def baseline_anti_favorite(market: dict) -> dict:
    """Bet NO on cheap longshots (YES ≤ 0.10), expecting longshot bias.

    We set model_prob low enough to clear the configured min_edge — i.e. our
    "model" believes the longshot is at least 4pp less likely than market does.
    """
    yes = market["yes_price"]
    if yes > 0.10:
        return {
            "model_prob": yes,
            "confidence": "skip",
            "reasoning": "[baseline:anti-favorite] only bets NO on YES ≤ 0.10",
            "claims": [],
        }
    # Force enough edge that the gate doesn't reject; cap at floor to avoid 0.
    mp = max(0.005, yes - 0.04)
    return {
        "model_prob": mp,
        "confidence": "low",
        "reasoning": "[baseline:anti-favorite] longshot premium hypothesis: NO @ low conf",
        "claims": [],
    }


def baseline_random(market: dict, seed_for_market: int, min_edge: float = 0.03) -> dict:
    """Pick YES or NO 50/50; deviation 8pp from market in chosen direction."""
    rng = random.Random(seed_for_market)
    bullish = rng.random() < 0.5
    yes = market["yes_price"]
    # Force a tradable edge so the baseline actually bets;
    # it's noise, but it has to clear the same gate the LLM does.
    delta = max(0.05, min_edge + 0.02)
    if bullish:
        mp = min(0.99, yes + delta)
    else:
        mp = max(0.01, yes - delta)
    return {
        "model_prob": mp,
        "confidence": "low",
        "reasoning": f"[baseline:random] coin-flipped {'YES' if bullish else 'NO'} with {delta:.0%} forced edge",
        "claims": [],
    }


BASELINES: dict[str, Callable[[dict, int], dict]] = {
    "always-skip": lambda m, _s: baseline_always_skip(m),
    "market-prob": lambda m, _s: baseline_market_prob(m),
    "anti-favorite": lambda m, _s: baseline_anti_favorite(m),
    "random": lambda m, s: baseline_random(m, s),
}


def run_baseline_decision_day(
    *,
    name: str,
    decision_dt: datetime,
    valid_markets: list[dict],
    available_cash: float,
    starting_capital: float,
    obj_by_slug: dict,
    seed: int = 0,
    config: Optional[dict] = None,
) -> tuple[list[Bet], list[dict]]:
    """Synchronous, deterministic baseline. No LLM, no SerpAPI, no traces.

    Returns the same (new_bets, decisions) tuple as _run_decision_day so the
    parent runner can plug it in interchangeably.
    """
    fn = BASELINES.get(name)
    if fn is None:
        raise ValueError(f"unknown baseline: {name}")
    kelly_params = params_from_config(config)

    # First pass: get verdicts and intended amounts (no portfolio cap yet)
    candidates = []  # (decision_entry, intended_amount, direction, edge, mp)
    decisions: list[dict] = []
    for i, m in enumerate(valid_markets):
        if name == "random":
            verdict = baseline_random(m, seed * 1000 + i, min_edge=kelly_params.min_edge)
        else:
            verdict = fn(m, seed * 1000 + i)
        mp = verdict["model_prob"]
        conf = verdict["confidence"]
        yes = m["yes_price"]
        edge = mp - yes if isinstance(mp, (int, float)) else None
        sizing = size_bet(
            model_prob=mp,
            market_prob=yes,
            confidence=conf,
            capital=starting_capital,
            config=config,
        )
        direction = sizing["direction"]
        intended_amount = sizing["amount"]
        edge = sizing["edge"]

        decision_entry = {
            "slug": m["slug"],
            "question": m["question"],
            "yes_price": yes,
            "no_price": m.get("no_price"),
            "volume": m.get("volume"),
            "end_date": m.get("end_date"),
            "category": m.get("category", ""),
            "direction": direction,
            "amount": intended_amount,
            "confidence": conf,
            "model_prob": mp,
            "market_prob": yes,
            "edge": edge,
            "reasoning": verdict.get("reasoning", ""),
            "baseline": name,
        }
        decisions.append(decision_entry)
        candidates.append((decision_entry, intended_amount, direction, edge, mp))

    # Portfolio cap (same logic as LLM path)
    PORTFOLIO_CAP_PCT = 0.50
    bet_candidates = [c for c in candidates if c[2] != "SKIP" and c[1] > 0]
    total_intended = sum(c[1] for c in bet_candidates)
    cap_total = starting_capital * PORTFOLIO_CAP_PCT
    if total_intended > cap_total > 0:
        scale = cap_total / total_intended
        for entry, *_ in bet_candidates:
            entry["original_amount"] = entry["amount"]
            entry["amount"] = round(entry["amount"] * scale, 2)
            entry["scaled_down"] = True

    # Realize bets, respecting cash
    cash_remaining = available_cash
    new_bets: list[Bet] = []
    for entry, _intended, direction, edge, mp in candidates:
        if direction == "SKIP":
            continue
        amount = entry["amount"]
        if amount < 1.0:
            entry["direction"] = "SKIP"
            entry["amount"] = 0.0
            continue
        max_stake = max_affordable_amount(cash_remaining)
        if amount > max_stake:
            amount = max_stake
            if amount < 1.0:
                entry["direction"] = "SKIP"
                entry["amount"] = 0.0
                continue
            entry["amount"] = amount
            entry["cash_clipped"] = True

        market_obj = obj_by_slug[entry["slug"]]
        bet = simulate_bet(
            market=market_obj, month=decision_dt.strftime("%Y-%m"),
            direction=direction, model_prob=mp, market_prob=entry["market_prob"],
            edge=edge,
            kelly_fraction=amount / starting_capital,
            capital=starting_capital,
        )
        bet.placed_at = decision_dt.isoformat()
        bet.settle_due_at = market_obj.end_date or None
        bet.resolution = None
        bet.pnl = None
        entry["entry_cost"] = round(bet.entry_cost, 2)
        entry["entry_fee"] = round(bet.entry_fee, 4)
        new_bets.append(bet)
        cash_remaining -= bet.entry_cost

    decisions.sort(key=lambda d: (d["direction"] == "SKIP", -d.get("amount", 0)))
    return new_bets, decisions


BASELINE_NAMES = list(BASELINES.keys())
