from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

from scripts.a_share_panic_index.providers import history, live
from scripts.a_share_panic_index.providers.base import ProviderDataError


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _EastmoneyClient:
    calls: list[dict] = []

    def __init__(self, _timeout):
        pass

    def get(self, _url, params, headers):
        self.calls.append(dict(params))
        secid = params["secid"]
        start = date.fromisoformat(params["beg"])
        end = date.fromisoformat(params["end"])
        market, code = secid.split(".")
        name = "Ａ股指数" if secid == "1.000002" else (
            "深证Ａ指" if secid == "0.399107" else "沪深300"
        )
        base_amount = 600_000_000_000 if market == "1" else 700_000_000_000
        rows = []
        current = start
        while current <= end:
            offset = (current - date(2026, 1, 1)).days
            rows.append(
                f"{current.isoformat()},10,11,12,9,1000,{base_amount + offset * 1000000}"
            )
            current += timedelta(days=1)
        return _Response(
            {
                "rc": 0,
                "data": {
                    "code": code,
                    "market": int(market),
                    "name": name,
                    "klines": rows,
                },
            }
        )


class _SinaClient:
    def __init__(self, _timeout):
        pass

    def get(self, _url, params):
        start = date(2026, 6, 1)
        rows = [
            {
                "day": (start + timedelta(days=index)).isoformat(),
                "close": str(3000 + index * index),
            }
            for index in range(35)
        ]
        return _Response(rows)


class TestHistorySources(unittest.TestCase):
    def setUp(self):
        _EastmoneyClient.calls = []

    def test_market_amount_combines_both_markets_and_incrementally_extends_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(history, "HttpClient", _EastmoneyClient):
            as_of = date(2026, 7, 1)
            first = history.fetch_market_amount_history_result(as_of, 30, 5)
            second = history.fetch_market_amount_history_result(as_of, 30, 5)
            extended = history.fetch_market_amount_history_result(as_of, 60, 5)

            self.assertTrue(first["available"])
            self.assertEqual(first["amount_unit"], "CNY")
            self.assertTrue(all(date.fromisoformat(row["date"]) < as_of for row in first["rows"]))
            expected_last = (
                1_300_000_000_000
                + 2 * ((as_of - timedelta(days=1)) - date(2026, 1, 1)).days * 1_000_000
            )
            self.assertEqual(first["rows"][-1]["amount"], expected_last)
            self.assertTrue(second["cache_hit"])
            self.assertEqual(len(_EastmoneyClient.calls), 4)
            self.assertEqual(extended["earliest_date"], (as_of - timedelta(days=60)).isoformat())
            self.assertTrue(all(call["end"] != "20500101" for call in _EastmoneyClient.calls))
            cache_files = list((Path(directory) / "history").glob("*.json"))
            self.assertEqual(len(cache_files), 2)

    def test_index_history_validates_identity_and_returns_required_shape(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(history, "HttpClient", _EastmoneyClient):
            rows = history.fetch_index_history(
                "sh000300",
                date(2026, 6, 1),
                date(2026, 6, 5),
                5,
            )
            self.assertEqual(len(rows), 5)
            self.assertEqual(
                set(rows[0]),
                {"date", "open", "high", "low", "close", "volume", "amount"},
            )
            self.assertEqual(rows[0]["date"], "2026-06-01")

    def test_wrong_index_identity_and_empty_range_are_rejected(self):
        wrong = {
            "code": "000001",
            "market": 1,
            "name": "错误指数",
            "rows": [{"date": "2026-06-01"}],
        }
        with self.assertRaises(ProviderDataError):
            history._validate_identity(wrong, "1.000300", None)
        empty = {"code": "000300", "market": 1, "name": "沪深300", "rows": []}
        with self.assertRaises(ProviderDataError):
            history._validate_identity(empty, "1.000300", None)

    def test_daily_baseline_excludes_as_of_day_for_volatility_and_amount(self):
        amount_rows = [
            {"date": (date(2026, 6, 1) + timedelta(days=index)).isoformat(), "amount": float(index + 1)}
            for index in range(25)
        ]
        amount_result = {
            "available": True,
            "rows": amount_rows,
            "amount_unit": "CNY",
            "latest_date": amount_rows[-1]["date"],
            "error": None,
        }
        with patch.object(live, "HttpClient", _SinaClient), patch.object(
            live,
            "fetch_market_amount_history_result",
            return_value=amount_result,
        ):
            result = live.fetch_sina_daily_baseline(
                {"symbol": "sh000300", "trade_date": "2026-07-01", "timeout": 5}
            )

        closes = np.array([3000 + index * index for index in range(30)], dtype=float)
        expected_sigma = float(np.log(closes[1:] / closes[:-1])[-20:].std(ddof=1))
        self.assertAlmostEqual(result["data"]["daily_sigma"], expected_sigma)
        self.assertEqual(result["data"]["latest_timestamp"], "2026-06-30T00:00:00")
        self.assertEqual(result["source_timestamp"][:10], "2026-06-30")
        self.assertEqual(result["data"]["median_daily_market_amount_20"], 15.5)
        self.assertFalse(result["provisional"])
        self.assertEqual(result["quality_flags"], [])

    def test_daily_baseline_keeps_amount_source_failure_transparent(self):
        unavailable = {
            "available": False,
            "rows": [],
            "amount_unit": "CNY",
            "error": "深圳A股: 数据源不可用",
        }
        with patch.object(live, "HttpClient", _SinaClient), patch.object(
            live,
            "fetch_market_amount_history_result",
            return_value=unavailable,
        ):
            result = live.fetch_sina_daily_baseline(
                {"symbol": "sh000300", "trade_date": "2026-07-01", "timeout": 5}
            )
        self.assertIsNone(result["data"]["median_daily_market_amount_20"])
        self.assertEqual(result["data"]["market_amount_history_error"], unavailable["error"])
        self.assertTrue(result["provisional"])
        self.assertIn("market_amount_history_unavailable", result["quality_flags"])


if __name__ == "__main__":
    unittest.main()
