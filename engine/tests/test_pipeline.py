from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.a_share_panic_index.pipeline.realtime import (
    RealtimePipeline,
    StaleDataError,
)
from tests.helpers import (
    NO_QVIX_FIXTURE,
    REALTIME_FIXTURE,
    make_database,
    now,
    settings,
    test_logger,
)


class TestRealtimePipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = make_database(Path(self.temp.name))
        self.pipeline = RealtimePipeline(settings(), self.database, test_logger())

    def tearDown(self):
        self.temp.cleanup()

    def test_realtime_result_has_raw_display_components_and_quality(self):
        result, _ = self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        self.assertEqual(result.snapshot_type, "realtime")
        self.assertEqual(result.finality, "provisional")
        self.assertEqual(set(result.components), {"volatility", "breadth", "derivatives", "liquidity"})
        self.assertGreaterEqual(result.coverage, 0.80)
        self.assertLessEqual(result.confidence, 75)
        self.assertEqual(result.reference_mode, "structural_bootstrap")
        self.assertEqual(result.source_skew_seconds, 0)

    def test_second_bucket_uses_fast_shock_and_bypasses_large_rise_smoothing(self):
        first, _ = self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        second, _ = self.pipeline.run(now(10, 5), fixture=str(REALTIME_FIXTURE))
        self.assertIsNotNone(second.feature_values["down_shock_5m_z"])
        self.assertGreaterEqual(second.realtime_panic_index_raw - first.realtime_panic_index, 8)
        self.assertEqual(second.realtime_panic_index, second.realtime_panic_index_raw)

    def test_qvix_missing_keeps_if_and_never_fills_fake_value(self):
        self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        result, _ = self.pipeline.run(now(10, 5), fixture=str(NO_QVIX_FIXTURE))
        self.assertIsNone(result.feature_values["qvix_level"])
        self.assertIn("qvix_unavailable", result.provisional_reasons)
        self.assertIn("derivatives", result.components)
        self.assertGreaterEqual(result.coverage, 0.80)
        self.assertLessEqual(result.confidence, 85)

    def test_lunch_break_freezes_last_value_without_new_row(self):
        first, _ = self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        result, meta = self.pipeline.run(now(12, 0), fixture=str(REALTIME_FIXTURE))
        self.assertIsNone(result)
        self.assertEqual(meta["status"], "lunch_break_frozen")
        self.assertEqual(meta["latest_realtime"]["realtime_panic_index"], first.realtime_panic_index)
        self.assertEqual(len(self.database.realtime_history("2026-07-24")), 1)

    def test_pre_open_and_non_trading_day_do_not_create_scores(self):
        result, meta = self.pipeline.run(now(9, 29), fixture=str(REALTIME_FIXTURE))
        self.assertIsNone(result)
        self.assertEqual(meta["status"], "market_not_ready")
        result, meta = self.pipeline.run(now(10, 0, day=25), fixture=str(REALTIME_FIXTURE))
        self.assertIsNone(result)
        self.assertEqual(meta["status"], "skipped_non_trading_day")
        self.assertEqual(self.database.realtime_history(), [])

    def test_same_five_minute_bucket_freezes_core_features(self):
        first, _ = self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        second, _ = self.pipeline.run(now(10, 1), fixture=str(REALTIME_FIXTURE))
        for name, value in first.feature_scores.items():
            if name != "down_shock_5m_z":
                self.assertEqual(second.feature_scores[name], value)

    def test_stale_core_source_rejects_new_score(self):
        source = json.loads((REALTIME_FIXTURE / "snapshot.json").read_text(encoding="utf-8"))
        source["providers"]["index"]["source_timestamp"] = "2026-07-24T09:50:00+08:00"
        path = Path(self.temp.name) / "stale.json"
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(StaleDataError):
            self.pipeline.run(now(10, 0), fixture=str(path))
        self.assertEqual(self.database.realtime_history(), [])

    def test_current_day_never_enters_same_time_history(self):
        self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        self.pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        history = self.database.same_bucket_history(6, now(10, 0).date())
        self.assertEqual(history, [])

    def test_after_close_sources_are_compared_at_the_1500_effective_time(self):
        source = json.loads(
            (REALTIME_FIXTURE / "1510.json").read_text(encoding="utf-8")
        )
        for semantic in ("index", "breadth", "limits"):
            source["providers"][semantic]["source_timestamp"] = (
                "2026-07-24T16:10:00+08:00"
            )
        path = Path(self.temp.name) / "after-close.json"
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        result, _ = self.pipeline.run(now(15, 10), fixture=str(path))
        self.assertEqual(result.source_skew_seconds, 0)


if __name__ == "__main__":
    unittest.main()
