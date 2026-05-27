"""Kelly Criterion for binary prediction markets."""
import logging

log = logging.getLogger("pm-backtest.kelly")


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
