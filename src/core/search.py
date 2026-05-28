"""Search context gathering — SerpAPI (Google) with absolute date filtering."""

import os
import re
import logging
import time
import requests
from typing import Optional
from datetime import datetime, timedelta

from dateutil import parser as dateparser

from .types import SearchContext
from .config import Cache

log = logging.getLogger("pm-backtest.info")

SERPAPI_BASE = "https://serpapi.com/search"


def _safe_request_error(exc: Exception) -> str:
    """Return a log-safe requests error without leaking api_key query params."""
    msg = str(exc)
    return re.sub(r"api_key=[^&\s]+", "api_key=<redacted>", msg)


def _parse_article_date(date_str: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a SerpAPI date string like 'Jan 15, 2026' or '2 days ago'.

    `now` controls the reference point for relative dates. In backtests this
    must be the search cutoff, not real wall-clock time, otherwise "2 days ago"
    leaks future articles into the historical window.
    """
    if not date_str:
        return None
    ref = now or datetime.now()
    try:
        # Handle relative dates (e.g. "2 days ago")
        if "ago" in date_str.lower():
            match = re.search(r'(\d+)\s*(day|week|month|year|hour|minute)s?\s*ago', date_str.lower())
            if match:
                n = int(match.group(1))
                unit = match.group(2)
                if unit.startswith('day'): return ref - timedelta(days=n)
                if unit.startswith('week'): return ref - timedelta(weeks=n)
                if unit.startswith('month'): return ref - timedelta(days=n * 30)
                if unit.startswith('year'): return ref - timedelta(days=n * 365)
                if unit.startswith('hour'): return ref - timedelta(hours=n)
                if unit.startswith('minute'): return ref - timedelta(minutes=n)
            return ref

        return dateparser.parse(date_str)
    except Exception:
        return None


def _filter_by_date(articles: list, cutoff_dt: datetime) -> tuple[list, int]:
    """Filter articles to only those published on or before cutoff_dt."""
    return _filter_by_date_policy(articles, cutoff_dt)


def _is_relative_date(date_str: str) -> bool:
    return bool(date_str and "ago" in date_str.lower())


def _filter_by_date_policy(
    articles: list,
    cutoff_dt: datetime,
    *,
    include_unknown: bool = True,
    include_relative: bool = True,
) -> tuple[list, int]:
    """Filter articles to cutoff with explicit policy for weak dates."""
    filtered = []
    skipped = 0
    for a in articles:
        date_str = a.get("date", "")
        if not date_str and not include_unknown:
            skipped += 1
            continue
        if _is_relative_date(date_str) and not include_relative:
            skipped += 1
            continue
        pub_dt = _parse_article_date(date_str, now=cutoff_dt)
        if pub_dt is None:
            # No date → include (can't prove it's after cutoff)
            filtered.append(a)
        elif pub_dt <= cutoff_dt:
            filtered.append(a)
        else:
            skipped += 1
            log.debug("DATE FILTER | skipped '%s' (date=%s > cutoff=%s)",
                      a.get("title", "")[:60], date_str, cutoff_dt.strftime("%Y-%m-%d"))
    return filtered, skipped


def _fmt_tbs_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to M/D/YYYY for SerpAPI tbs parameter."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}/{dt.year}"


def _extract_keywords(question: str) -> str:
    """Extract good search keywords from a market question."""
    prefixes = ["Will ", "Who will ", "What will ", "How many ", "Will the "]
    text = question
    for p in prefixes:
        if text.startswith(p):
            text = text[len(p):]

    patterns = [" on ", " by ", " in January", " in February", " in March",
                " in April", " in May", " in June", " in July", " in August",
                " in September", " in October", " in November", " in December",
                " before ", " after ", " between "]
    for pat in patterns:
        idx = text.find(pat)
        if idx > 10:
            text = text[:idx]
            break

    words = text.split()
    if len(words) > 8:
        text = " ".join(words[:8])

    return text.strip()


def search_context(
    query: str,
    end_date: str,
    serpapi_api_key: str,
    cache: Optional[Cache] = None,
    max_results: int = 5,
) -> SearchContext:
    """
    Search via SerpAPI (Google) with absolute date filtering using tbs parameter.
    Date format: M/D/YYYY for tbs (e.g., 1/31/2025).
    """
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=60)
    start_date = start_dt.strftime("%Y-%m-%d")

    keywords = _extract_keywords(query)
    search_queries = [keywords]
    if keywords != query[:80]:
        search_queries.append(query[:80])

    for sq in search_queries:
        # cache key intentionally omits max_results: the SerpAPI response is the
        # same regardless of slice size, and including it would miss-cache
        # whenever the caller bumps max_results.
        cache_key = ("serpapi", sq[:100], start_date, end_date)

        if cache:
            cached = cache.get(*cache_key)
            if cached:
                log.debug("CACHE HIT | serpapi='%s'", sq[:60])
                return SearchContext(**cached)

        # tbs date filter at API level — often too restrictive, rely on _filter_by_date instead
        log.info("SERPAPI | '%s' | cutoff=%s", sq[:80], end_date)

        params = {
            "api_key": serpapi_api_key,
            "engine": "google",
            "q": sq,
            "num": max_results * 2,  # fetch more, filter by date later
            "tbs": (
                f"cdr:1,cd_min:{_fmt_tbs_date(start_date)},"
                f"cd_max:{_fmt_tbs_date(end_date)}"
            ),
        }

        for mode, mode_params, filter_kwargs in [
            ("tbs", params, {"include_unknown": True, "include_relative": True}),
            (
                "fallback-no-tbs",
                {k: v for k, v in params.items() if k != "tbs"},
                {"include_unknown": False, "include_relative": False},
            ),
        ]:
            data = None
            for attempt in range(1, 4):
                try:
                    proxies = None
                    if os.environ.get("HTTPS_PROXY"):
                        proxies = {"https": os.environ["HTTPS_PROXY"]}
                    resp = requests.get(
                        SERPAPI_BASE,
                        params=mode_params,
                        proxies=proxies,
                        timeout=20,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else None
                    log.error("SERPAPI ERR | '%s' | status=%s | %s",
                              sq[:60], status, _safe_request_error(e))
                    if status in (401, 403):
                        break
                except (requests.ConnectionError, requests.Timeout,
                        requests.exceptions.SSLError) as e:
                    log.warning("SERPAPI RETRY | '%s' | attempt=%d/3 | %s",
                                sq[:60], attempt, _safe_request_error(e))
                    if attempt < 3:
                        time.sleep(1.5 * attempt)
                except Exception as e:
                    log.error("SERPAPI ERR | '%s': %s", sq[:60], _safe_request_error(e))
                    break
            if data is None:
                continue

            organic = data.get("organic_results", [])
            log.debug("SERPAPI | %s | %d raw results for '%s'",
                      mode, len(organic), sq[:60])
            if mode == "tbs" and not organic:
                log.info("SERPAPI | tbs returned 0, retrying without tbs for '%s'",
                         sq[:60])

            # Enrich with date field and apply date filter
            enriched = [{"title": r.get("title", ""), "snippet": r.get("snippet", ""),
                         "link": r.get("link", ""), "date": r.get("date", ""),
                         "date_unknown": not bool(r.get("date", "")),
                         "search_mode": mode}
                        for r in organic]
            filtered, skipped = _filter_by_date_policy(
                enriched,
                end_dt,
                **filter_kwargs,
            )
            if skipped:
                log.info("SERPAPI | %s date-filtered: %d kept / %d skipped (before %s)",
                         mode, len(filtered), skipped, end_dt.strftime("%Y-%m-%d"))

            if filtered:
                parts = [f"## {r['title']}\n{r['snippet']}" for r in filtered]

                summary = "\n\n".join(parts)
                ctx = SearchContext(
                    query=sq,
                    end_date=end_date,
                    results=filtered,
                    summary=summary,
                )

                if cache:
                    cache.set({
                        "query": ctx.query,
                        "end_date": ctx.end_date,
                        "results": ctx.results,
                        "summary": ctx.summary,
                    }, *cache_key)
                return ctx

        log.debug("SERPAPI | no results for '%s', trying next query", sq[:60])

    log.warning("SERPAPI | all %d queries returned nothing for '%s'", len(search_queries), query[:80])
    return SearchContext(query=query, end_date=end_date, results=[], summary="(no results found)")
