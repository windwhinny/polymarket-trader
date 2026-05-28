"""Kelly Criterion for binary prediction markets."""
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("pm-backtest.kelly")

CONFIDENCE_MULTIPLIERS = {
    "high": 1.0,
    "medium": 0.5,
    "low": 0.25,
}


@dataclass(frozen=True)
class KellyParams:
    """Risk parameters for sizing a binary-market bet."""
    fraction: float = 1.0
    min_edge: float = 0.03
    max_bet_pct: float = 0.15


def params_from_config(config: Optional[dict] = None) -> KellyParams:
    raw = (config or {}).get("kelly", {}) or {}
    return KellyParams(
        fraction=float(raw.get("fraction", 1.0)),
        min_edge=float(raw.get("min_edge", 0.03)),
        max_bet_pct=float(raw.get("max_bet_pct", 0.15)),
    )


def size_bet(
    *,
    model_prob,
    market_prob: float,
    confidence: str,
    capital: float,
    config: Optional[dict] = None,
) -> dict:
    """Convert model probability + confidence into direction and stake.

    The LLM only supplies probability and confidence. All risk knobs come from
    config so backtest, predict, and baselines share the same sizing behavior.
    """
    params = params_from_config(config)
    try:
        mp = max(0.01, min(0.99, float(model_prob)))
        yes = max(0.01, min(0.99, float(market_prob)))
    except (TypeError, ValueError):
        return {
            "direction": "SKIP",
            "amount": 0.0,
            "edge": None,
            "kelly_fraction": 0.0,
            "reason": "invalid probability",
        }

    conf = (confidence or "skip").lower()
    conf_mult = CONFIDENCE_MULTIPLIERS.get(conf)
    edge = mp - yes
    if conf_mult is None:
        return {
            "direction": "SKIP",
            "amount": 0.0,
            "edge": edge,
            "kelly_fraction": 0.0,
            "reason": "confidence not tradable",
        }
    if abs(edge) < params.min_edge:
        return {
            "direction": "SKIP",
            "amount": 0.0,
            "edge": edge,
            "kelly_fraction": 0.0,
            "reason": "edge below min_edge",
        }

    if edge > 0:
        direction = "YES"
        kelly_raw = edge / (1 - yes) if yes < 1 else 0.0
    else:
        direction = "NO"
        kelly_raw = (-edge) / yes if yes > 0 else 0.0

    kelly_fraction = max(0.0, kelly_raw) * conf_mult * params.fraction
    kelly_fraction = min(kelly_fraction, params.max_bet_pct)
    amount = round(max(0.0, float(capital)) * kelly_fraction, 2)
    if amount < 1.0:
        return {
            "direction": "SKIP",
            "amount": 0.0,
            "edge": edge,
            "kelly_fraction": kelly_fraction,
            "reason": "amount below minimum",
        }

    return {
        "direction": direction,
        "amount": amount,
        "edge": edge,
        "kelly_fraction": kelly_fraction,
        "reason": "ok",
    }


def kelly_bet(
    model_prob: float,
    market_prob: float,
    fraction: float = 0.5,
    min_edge: float = 0.03,
    max_bet_pct: float = 0.25,
) -> tuple[str, float, float]:
    """
    Calculate Kelly-optimal bet.

    Returns: (direction, kelly_fraction, edge)
      direction: "YES", "NO", or "SKIP"
      kelly_fraction: fraction of capital to bet (0 if SKIP)
      edge: absolute difference between model and market probabilities
    """
    edge = abs(model_prob - market_prob)

    if edge < min_edge:
        log.debug("KELLY SKIP | edge=%.4f < min=%.4f", edge, min_edge)
        return ("SKIP", 0.0, edge)

    if model_prob > market_prob:
        # Bet YES
        kelly = (model_prob - market_prob) / (1 - market_prob)
        direction = "YES"
    else:
        # Bet NO
        kelly = (market_prob - model_prob) / market_prob
        direction = "NO"

    kelly = max(0.0, min(kelly, 1.0))
    kelly *= fraction
    kelly = min(kelly, max_bet_pct)

    log.debug("KELLY | %s | model=%.4f market=%.4f edge=%.4f kelly_raw=%.4f kelly_adj=%.4f",
              direction, model_prob, market_prob, edge, abs(model_prob - market_prob) / (1 - market_prob if model_prob > market_prob else market_prob), kelly)

    return (direction, kelly, edge)
