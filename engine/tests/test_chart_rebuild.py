from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from scripts.a_share_panic_index.chart import generate_chart
from scripts.a_share_panic_index.features.daily import build_daily_feature_values
from scripts.a_share_panic_index.models import DailyResult
from scripts.a_share_panic_index.pipeline.rebuild import RebuildPipeline
from tests.helpers import make_database, settings, test_logger


def raw_record(trade_date: date, offset: int = 0) -> dict:
    previous_close = 4000.0 + offset
    close = previous_close * (0.995 + (offset % 3) * 0.002)
    return {
        "trade_date": trade_date.isoformat(),
        "open": previous_close * 0.998,
        "high": previous_close * 1.01,
        "low": previous_close * 0.98,
        "close": close,
        "previous_close": previous_close,
        "up_count": 2100,
        "down_count": 2800,
        "flat_count": 100,
        "valid_stock_count": 5000,
        "decline_share": 0.56,
        "decline_5_share": 0.025,
        "decline_7_share": 0.008,
        "median_return": -0.004,
        "limit_up": 45,
        "limit_down": 18,
        "market_amount": 1_000_000_000_000.0 + offset * 1_000_000_000.0,
        "daily_sigma": 0.012,
        "front_contract": "IF2608",
        "front_annualized_basis": -0.02,
        "next_annualized_basis": -0.01,
        "basis_curve_stress": 0.01,
        "basis_expansion_3d": 0.002,
        "qvix": 21.0,
        "qvix_daily_change": 0.01,
        "sources": {"fixture": {"source_timestamp": trade_date.isoformat()}},
    }


class TestChartAndRebuild(unittest.TestCase):
    def test_short_daily_history_does_not_invent_zero_volatility(self):
        current = raw_record(date(2026, 7, 24))
        values = build_daily_feature_values(current, [])
        self.assertIsNone(values["ewma_volatility_5"])
        self.assertIsNone(values["realized_volatility_20"])
        self.assertIsNone(values["downside_volatility_20"])
        self.assertIsNone(values["parkinson_volatility_10"])

    def test_daily_chart_uses_one_year_window_without_filling_missing_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = make_database(root)
            for trade_date, score in (
                (date(2026, 1, 5), 42.0),
                (date(2026, 7, 24), 61.0),
            ):
                result = DailyResult(
                    trade_date=trade_date,
                    final_panic_index=score,
                    level="中性",
                    components={"volatility": score},
                    feature_values={},
                    feature_scores={},
                    confidence=100.0,
                    coverage=1.0,
                    quality_status="complete",
                    source_timestamps={},
                )
                database.write_daily(raw_record(trade_date), result)
            output = root / "近一年真实记录.png"
            report = generate_chart(database, output, "daily", as_of_date=date(2026, 7, 24))
            self.assertEqual(report["period"], "trailing_1_year")
            self.assertEqual(report["records"], 2)
            self.assertFalse(report["coverage_complete"])
            self.assertFalse(report["missing_dates_filled"])
            self.assertEqual(report["start_date"], "2026-01-05")
            self.assertTrue(output.exists())

    def test_rebuild_fixture_is_walk_forward_and_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = make_database(root)
            start = date(2026, 5, 4)
            records = [raw_record(start + timedelta(days=index), index) for index in range(30)]
            records.reverse()
            fixture = root / "daily_history.json"
            fixture.write_text(
                json.dumps({"records": records}, ensure_ascii=False),
                encoding="utf-8",
            )
            report = RebuildPipeline(
                settings(), database, test_logger()
            ).run(str(fixture))
            history = database.daily_history(limit=100)
            self.assertEqual(report["rebuilt_days"], 30)
            self.assertEqual(history[0]["trade_date"], "2026-05-04")
            self.assertEqual(history[-1]["trade_date"], "2026-06-02")
            future = database.daily_feature_history(date(2026, 5, 10))
            self.assertTrue(all(item["trade_date"] < "2026-05-10" for item in future))


if __name__ == "__main__":
    unittest.main()
