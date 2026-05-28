"""Polymarket CLOB API — fetch historical prices for tokens."""
import requests
import time
import logging
from typing import Optional

from .types import PriceSnapshot
from .config import Cache

log = logging.getLogger("pm-backtest.price")

CLOB_BASE = "https://clob.polymarket.com"


def fetch_price_at_time(
    token_id: str,
    target_ts: int,
    cache: Optional[Cache] = None,
    max_age_seconds: Optional[int] = None,
) -> Optional[float]:
    """
    Fetch the latest price at or before a target timestamp.

    Uses CLOB /prices-history with interval=max to get all available data,
    then finds the most recent data point that does not peek past target_ts.
    """
    if not token_id:
        return None

    cache_key = ("price", token_id, str(target_ts), str(max_age_seconds or ""))

    if cache:
        cached = cache.get(*cache_key)
        if cached is not None:
            log.debug("CACHE HIT | price %s @ ts=%d", token_id[:12], target_ts)
            return cached

    log.debug("FETCH | price history for token %s", token_id[:16])

    try:
        url = f"{CLOB_BASE}/prices-history"
        params = {
            "market": token_id,
            "interval": "max",
            "fidelity": 1440,  # daily data points
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("API ERROR | prices-history %s: %s", token_id[:16], e)
        return None

    history = data.get("history", [])
    if not history:
        log.warning("NO HISTORY | token %s", token_id[:16])
        return None

    log.debug("GOT | %d data points for token %s", len(history), token_id[:16])

    # Find the last data point at or before target_ts. Using the closest point
    # can select a future price and leak post-decision information.
    closest = None
    min_diff = float("inf")
    for point in history:
        t = point.get("t")
        if t is None or t > target_ts:
            continue
        diff = target_ts - t
        if diff < min_diff:
            min_diff = diff
            closest = point

    if closest is None:
        log.warning("NO PAST PRICE | token %s before ts=%d", token_id[:16], target_ts)
        return None

    if max_age_seconds is not None and min_diff > max_age_seconds:
        log.warning("STALE PRICE | token %s age=%ds > max=%ds",
                    token_id[:16], int(min_diff), max_age_seconds)
        return None

    price = float(closest["p"])
    log.debug("PRICE | token %s @ ts=%d → %.4f (age=%ds)",
              token_id[:12], target_ts, price, int(min_diff))

    if cache:
        cache.set(price, *cache_key)

    return price


def fetch_prices_at(
    token_ids: list[str],
    decision_dt,
    cache: Optional[Cache] = None,
    request_delay: float = 0.3,
    max_age_seconds: Optional[int] = None,
) -> dict[str, Optional[float]]:
    """Fetch each token's latest price at or before `decision_dt`."""
    target_ts = int(decision_dt.timestamp())
    log.info("FETCHING prices for %d tokens at %s", len(token_ids), decision_dt.isoformat())
    results = {}
    import time
    for i, tid in enumerate(token_ids):
        if not tid:
            results[tid] = None
            continue
        if i > 0:
            time.sleep(request_delay)
        results[tid] = fetch_price_at_time(tid, target_ts, cache, max_age_seconds)
    valid = sum(1 for v in results.values() if v is not None)
    log.info("RESULT | %d/%d prices fetched", valid, len(token_ids))
    return results


def fetch_prices_at_month_end(
    token_ids: list[str],
    year: int,
    month: int,
    cache: Optional[Cache] = None,
    request_delay: float = 0.5,
) -> dict[str, Optional[float]]:
    """Fetch prices for multiple tokens at month end."""
    from datetime import datetime, timedelta

    if month == 12:
        dt = datetime(year + 1, 1, 1) - timedelta(seconds=1)
    else:
        dt = datetime(year, month + 1, 1) - timedelta(seconds=1)

    target_ts = int(dt.timestamp())

    log.info("FETCHING prices for %d tokens at %s (ts=%d)", len(token_ids), dt.isoformat(), target_ts)

    results = {}
    for i, tid in enumerate(token_ids):
        if not tid:
            results[tid] = None
            continue
        if i > 0:
            time.sleep(request_delay)
        results[tid] = fetch_price_at_time(tid, target_ts, cache)

    valid = sum(1 for v in results.values() if v is not None)
    log.info("RESULT | %d/%d prices fetched successfully", valid, len(token_ids))
    return results
