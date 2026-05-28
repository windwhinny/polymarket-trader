import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.core.kelly import size_bet
from src.core.price_data import fetch_price_at_time
from src.core.search import search_context
from src.core.simulator import (
    cash_outlay_for_bet,
    entry_cost_for_amount,
    max_affordable_amount,
    settle_bet,
    simulate_bet,
)
from src.core.types import Market


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CoreBoundaryTests(unittest.TestCase):
    def test_price_lookup_uses_last_past_point_not_future_point(self):
        payload = {
            "history": [
                {"t": 1_000, "p": 0.20},
                {"t": 2_000, "p": 0.80},
            ]
        }

        with patch("src.core.price_data.requests.get", return_value=FakeResponse(payload)):
            self.assertEqual(fetch_price_at_time("token", 1_500), 0.20)
            self.assertIsNone(fetch_price_at_time("token", 900))

    def test_price_lookup_can_reject_stale_points(self):
        payload = {"history": [{"t": 1_000, "p": 0.20}]}

        with patch("src.core.price_data.requests.get", return_value=FakeResponse(payload)):
            self.assertIsNone(
                fetch_price_at_time("token", 1_500, max_age_seconds=100)
            )

    def test_serpapi_request_uses_absolute_cutoff_window(self):
        seen_params = {}

        def fake_get(_url, params, proxies, timeout):
            seen_params.update(params)
            return FakeResponse({
                "organic_results": [
                    {
                        "title": "before cutoff",
                        "snippet": "usable",
                        "link": "https://example.com/a",
                        "date": "Apr 1, 2026",
                    },
                    {
                        "title": "after cutoff",
                        "snippet": "must be filtered",
                        "link": "https://example.com/b",
                        "date": "Apr 20, 2026",
                    },
                ]
            })

        with patch("src.core.search.requests.get", side_effect=fake_get):
            ctx = search_context("test market question", "2026-04-15", "key")

        self.assertIn("tbs", seen_params)
        self.assertIn("cd_min:2/14/2026", seen_params["tbs"])
        self.assertIn("cd_max:4/15/2026", seen_params["tbs"])
        self.assertEqual([r["title"] for r in ctx.results], ["before cutoff"])

    def test_serpapi_fallback_excludes_weak_dates(self):
        seen_modes = []

        def fake_get(_url, params, proxies, timeout):
            seen_modes.append("tbs" if "tbs" in params else "no_tbs")
            if "tbs" in params:
                return FakeResponse({"organic_results": []})
            return FakeResponse({
                "organic_results": [
                    {
                        "title": "future absolute",
                        "snippet": "must be filtered",
                        "link": "https://example.com/future",
                        "date": "Apr 29, 2026",
                    },
                    {
                        "title": "relative date",
                        "snippet": "unsafe without tbs",
                        "link": "https://example.com/relative",
                        "date": "8 days ago",
                    },
                    {
                        "title": "unknown date",
                        "snippet": "unsafe without tbs",
                        "link": "https://example.com/unknown",
                        "date": "",
                    },
                    {
                        "title": "past absolute",
                        "snippet": "usable",
                        "link": "https://example.com/past",
                        "date": "Mar 10, 2026",
                    },
                ]
            })

        with patch("src.core.search.requests.get", side_effect=fake_get):
            ctx = search_context("query with strict fallback", "2026-04-01", "key")

        self.assertEqual(seen_modes, ["tbs", "no_tbs"])
        self.assertEqual([r["title"] for r in ctx.results], ["past absolute"])
        self.assertEqual(ctx.results[0]["search_mode"], "fallback-no-tbs")

    def test_simulated_bet_tracks_full_entry_cash_outlay(self):
        market = Market(
            id="m1",
            condition_id="c1",
            question="Will X happen?",
            slug="will-x",
            outcomes=["Yes", "No"],
            token_ids=["yes", "no"],
            volume=1_000,
            start_date="2026-01-01",
            end_date="2026-01-31",
            closed=True,
            resolution="YES",
        )
        bet = simulate_bet(
            market=market,
            month="2026-01",
            direction="YES",
            model_prob=0.65,
            market_prob=0.50,
            edge=0.15,
            kelly_fraction=0.10,
            capital=1_000,
        )

        self.assertGreater(bet.entry_cost, bet.amount)
        self.assertAlmostEqual(bet.entry_cost, entry_cost_for_amount(bet.amount))
        settle_bet(bet, market)

        gross_return = bet.amount / bet.entry_price
        self.assertAlmostEqual(cash_outlay_for_bet(bet) + bet.pnl, gross_return)

    def test_max_affordable_amount_accounts_for_fee_and_gas(self):
        stake = max_affordable_amount(100)

        self.assertLessEqual(entry_cost_for_amount(stake), 100)
        self.assertGreater(stake, 99)

    def test_kelly_sizing_uses_configured_risk_params(self):
        config = {
            "kelly": {
                "fraction": 0.5,
                "min_edge": 0.10,
                "max_bet_pct": 0.05,
            }
        }

        below_edge = size_bet(
            model_prob=0.59,
            market_prob=0.50,
            confidence="high",
            capital=1_000,
            config=config,
        )
        self.assertEqual(below_edge["direction"], "SKIP")

        capped = size_bet(
            model_prob=0.90,
            market_prob=0.50,
            confidence="high",
            capital=1_000,
            config=config,
        )
        self.assertEqual(capped["direction"], "YES")
        self.assertEqual(capped["amount"], 50.0)


if __name__ == "__main__":
    unittest.main()
