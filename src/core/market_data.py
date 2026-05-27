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


def _extract_resolution(raw: dict, prices: list) -> Optional[str]:
    """Best-effort resolution extraction for a closed Gamma market.

    Sources tried, in order:
      1. umaResolutionStatuses — Polymarket's authoritative oracle status
      2. resolvedBy / resolution fields on the raw payload
      3. Final outcomePrices — only as a fallback, with a relaxed threshold
    """
    # 1. UMA resolution status (list of dicts, one per outcome)
    uma = raw.get("umaResolutionStatuses")
    if isinstance(uma, str):
        try:
            uma = json.loads(uma)
        except Exception:
            uma = None
    if isinstance(uma, list):
        for entry in uma:
            if isinstance(entry, dict) and entry.get("status") == "resolved":
                outcome = entry.get("outcome") or entry.get("payout")
                if outcome and str(outcome).upper() in ("YES", "NO"):
                    return str(outcome).upper()

    # 2. Direct resolution field if Gamma sets one
    direct = raw.get("resolution") or raw.get("resolvedOutcome")
    if isinstance(direct, str) and direct.upper() in ("YES", "NO"):
        return direct.upper()

    # 3. Fall back to terminal prices, but allow 0.95 instead of 0.999.
    #    Closed markets occasionally settle slightly off the rail in the snapshot.
    if prices and len(prices) == 2:
        try:
            p0, p1 = float(prices[0]), float(prices[1])
        except (TypeError, ValueError):
            return None
        if p0 >= 0.95 and p1 <= 0.05:
            return "YES"
        if p1 >= 0.95 and p0 <= 0.05:
            return "NO"

    return None


def fetch_markets_active_at(
    decision_dt: datetime,
    horizon_days: int = 90,
    min_volume: float = 50000,
    page_limit: int = 100,
    max_pages: int = 8,
    request_delay: float = 0.3,
    cache: Optional[Cache] = None,
) -> list[Market]:
    """Fetch markets that were active at `decision_dt`.

    A market qualifies if:
      - it has resolved (so we can settle it later in the backtest)
      - resolution date is AFTER decision_dt (i.e., it was open at decision time)
      - resolution date is within `horizon_days` of decision_dt (avoid betting on
        events that won't settle for years; aligns with the "near-term" horizon
        the prompt promises)
      - createdAt <= decision_dt (the market actually existed)
      - volume > min_volume

    This replaces the month-aligned `fetch_markets` which had a survivorship
    bias toward markets resolving exactly at month-end.
    """
    decision_key = decision_dt.strftime("%Y-%m-%d")
    cache_key = ("markets-active", decision_key, str(horizon_days), str(min_volume))

    if cache:
        cached = cache.get(*cache_key)
        if cached:
            log.info("CACHE HIT | active@%s | %d markets", decision_key, len(cached))
            return [_dict_to_market(m) for m in cached]

    horizon_dt = decision_dt + timedelta(days=horizon_days)
    log.info("FETCHING active@%s | horizon=%dd min_vol=%.0f",
             decision_key, horizon_days, min_volume)

    markets: list[Market] = []
    seen_ids: set[str] = set()

    for page in range(max_pages):
        offset = page * page_limit
        params = {
            "closed": "true",
            "limit": page_limit,
            "offset": offset,
            "order": "volumeNum",
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
            break

        for raw in data:
            m = _parse_market(raw)
            if m is None or m.id in seen_ids:
                continue
            if m.volume < min_volume:
                continue

            end_dt = _parse_iso_utc(raw.get("endDate", raw.get("closedTime", "")))
            if end_dt is None:
                continue
            if end_dt <= decision_dt:
                # Already resolved before decision day → can't trade on it then
                continue
            if end_dt > horizon_dt:
                continue
            if m.resolution is None:
                # Need a known resolution to settle the bet later in the backtest
                continue

            start_dt = _parse_iso_utc(raw.get("createdAt", raw.get("startDate", "")))
            if start_dt is not None and start_dt > decision_dt:
                continue  # market didn't exist yet

            seen_ids.add(m.id)
            markets.append(m)

        batch_vol = [float(d.get("volume", 0)) for d in data]
        if batch_vol and max(batch_vol) < min_volume:
            break

        time.sleep(request_delay)

    log.info("RESULT active@%s | %d markets (resolving %s..%s)",
             decision_key, len(markets),
             decision_dt.date(), horizon_dt.date())

    if cache:
        cache.set([_market_to_dict(m) for m in markets], *cache_key)

    return markets


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
            "order": "volumeNum",
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

        resolution = _extract_resolution(raw, prices)

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
