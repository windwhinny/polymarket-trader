"""Search backend factories — return a SearchFn closure.

A SearchFn takes (query, cutoff_iso_or_None) and returns a list of
{title, snippet, date, source} dicts. Factories own caching, rate-limit
handling, and provider-specific quirks; the analyzer just calls the function.
"""

import logging
from datetime import datetime
from typing import Callable, Optional

from .config import Cache
from .search import search_context

log = logging.getLogger("pm-trader.search_backend")


SearchFn = Callable[[str, Optional[str]], list[dict]]


def make_serpapi_backend(serpapi_key: str, cache: Cache):
    """Date-filtered Google search via SerpAPI. Right backend for backtests."""

    def fn(query: str, cutoff_iso: Optional[str]) -> list[dict]:
        if not cutoff_iso:
            cutoff_date = datetime.utcnow().strftime("%Y-%m-%d")
        else:
            cutoff_date = cutoff_iso[:10]
        ctx = search_context(
            query=query,
            end_date=cutoff_date,
            serpapi_api_key=serpapi_key,
            cache=cache,
            max_results=5,
        )
        out = []
        for r in (ctx.results or []):
            out.append({
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "date": r.get("date", ""),
                "source": r.get("link", ""),
            })
        return out

    return fn


def make_tavily_backend(tavily_key: str):
    """Real-time Tavily search. Right backend for predict mode."""
    from tavily import TavilyClient
    client = TavilyClient(api_key=tavily_key)

    def fn(query: str, cutoff_iso: Optional[str]) -> list[dict]:
        try:
            resp = client.search(query=query, search_depth="basic", max_results=5)
        except Exception as e:
            log.warning("Tavily error for %r: %s", query[:60], e)
            return []
        out = []
        for r in (resp.get("results") or []):
            out.append({
                "title": r.get("title", ""),
                "snippet": r.get("content", ""),
                "date": r.get("published_date", ""),
                "source": r.get("url", ""),
            })
        return out

    return fn
