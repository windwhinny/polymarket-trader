"""Polymarket Gamma API — fetch resolved markets by month, with date filtering."""
import requests
import time
import logging
from datetime import datetime, timedelta
from typing import Optional
import json
from dateutil import parser as dateparser

from .types import Market
from .config import Cache

log = logging.getLogger("pm-backtest.market")

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _parse_iso_utc(raw: str) -> Optional[datetime]:
    """Parse various ISO datetime formats to UTC datetime."""
    if not raw:
        return None
    try:
        dt = dateparser.parse(raw)
        if dt is None:
            return None
        from datetime import timezone
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_markets(
    year: int,
    month: int,
    min_volume: float = 50000,
    page_limit: int = 50,
    max_pages: int = 4,
    request_delay: float = 0.3,
    cache: Optional[Cache] = None,
) -> list[Market]:
    """
    Fetch resolved markets that were still unresolved at end of the target month.
    Only returns markets where resolution date > month end date.
    """
    month_key = f"{year}-{month:02d}"
    cache_key = ("markets-v2", month_key, str(min_volume))

    if cache:
        cached = cache.get(*cache_key)
        if cached:
            log.info("CACHE HIT | %s | %d markets", month_key, len(cached))
            return [_dict_to_market(m) for m in cached]

    # Month end boundary: markets must resolve AFTER this date to be included
    from datetime import timezone
    if month == 12:
        month_end_dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    else:
        month_end_dt = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)

    log.info("FETCHING | %s | cutoff=%s min_vol=%.0f pages<=%d",
             month_key, month_end_dt.isoformat(), min_volume, max_pages)

    markets = []

    for page in range(max_pages):
        offset = page * page_limit
        params = {
            "closed": "true",
            "limit": page_limit,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }

        try:
            resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error("API ERROR | page=%d offset=%d: %s", page, offset, e)
            break

        if not data:
            log.debug("API END | empty page at offset=%d", offset)
            break

        for raw in data:
            m = _parse_market(raw)
            if m is None:
                continue
            if m.volume < min_volume:
                continue

            # Must have a resolution date after month end
            end_dt = _parse_iso_utc(raw.get("endDate", raw.get("closedTime", "")))
            if end_dt is None:
                continue
            if end_dt <= month_end_dt:
                log.debug("SKIP (resolved) | %s | end=%s <= cutoff=%s",
                          m.slug, end_dt.isoformat(), month_end_dt.isoformat())
                continue
            if m.resolution is None:
                continue

            markets.append(m)

        batch_vol = [float(d.get("volume", 0)) for d in data]
        log.debug("PAGE %d | offset=%d count=%d vol_range=%.0f-%.0f total_qual=%d",
                  page, offset, len(data), min(batch_vol) if batch_vol else 0,
                  max(batch_vol) if batch_vol else 0, len(markets))

        if batch_vol and max(batch_vol) < min_volume:
            log.debug("EARLY STOP | max vol %.0f < min %.0f", max(batch_vol), min_volume)
            break

        time.sleep(request_delay)

    log.info("RESULT | %s | %d qualifying markets (resolving after %s)",
             month_key, len(markets), month_end_dt.date())

    if cache:
        cache.set([_market_to_dict(m) for m in markets], *cache_key)

    return markets


def _parse_market(raw: dict) -> Optional[Market]:
    try:
        outcomes_raw = raw.get("outcomes", "[]")
        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw

        if len(outcomes) != 2:
            return None

        prices_raw = raw.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw

        token_ids = raw.get("clobTokenIds", "[]")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)

        resolution = None
        if prices and len(prices) == 2:
            if float(prices[0]) >= 0.999:
                resolution = "YES"
            elif float(prices[1]) >= 0.999:
                resolution = "NO"

        return Market(
            id=raw.get("id", ""),
            condition_id=raw.get("conditionId", ""),
            question=raw.get("question", raw.get("title", "")),
            slug=raw.get("slug", ""),
            outcomes=outcomes,
            token_ids=token_ids if token_ids else ["", ""],
            volume=float(raw.get("volume", 0)),
            start_date=raw.get("createdAt", raw.get("startDate", "")),
            end_date=raw.get("endDate", raw.get("closedTime", "")),
            closed=raw.get("closed", False),
            resolution=resolution,
            category=raw.get("category", ""),
            outcome_prices=[float(p) for p in prices] if prices else [],
        )
    except Exception as e:
        log.warning("PARSE ERR | %s: %s", raw.get("slug", "?"), e)
        return None


def _market_to_dict(m: Market) -> dict:
    from dataclasses import asdict
    return asdict(m)


def _dict_to_market(d: dict) -> Market:
    return Market(**d)
