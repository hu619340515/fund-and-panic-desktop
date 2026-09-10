from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.a_share_panic_index.logging_utils import configure_logging
from scripts.a_share_panic_index.pipeline.realtime import RealtimePipeline
from scripts.a_share_panic_index.providers.base import (
    ProviderDataError,
    ProviderTimeout,
    ProviderUnavailable,
)
from scripts.a_share_panic_index.providers.registry import ProviderManager
from tests.helpers import make_database, now, settings, test_logger


class TestReliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = make_database(Path(self.temp.name))
        self.manager = ProviderManager(settings(), self.database, test_logger())
        self.manager.network["retry_delay_seconds"] = 0

    def tearDown(self):
        self.temp.cleanup()

    def test_retryable_error_retries_but_data_error_switches_immediately(self):
        success = {"latency_ms": 1.0, "data": {}}
        with patch(
            "scripts.a_share_panic_index.providers.registry.run_with_hard_timeout",
            side_effect=[ProviderTimeout("超时"), success],
        ) as mocked:
            result, events = self.manager._fetch_specific(
                "test", "breadth", {"timeout": 1}, time.monotonic()
            )
        self.assertIs(result, success)
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(events[0]["retryable"])

        with patch(
            "scripts.a_share_panic_index.providers.registry.run_with_hard_timeout",
            side_effect=ProviderDataError("字段错误"),
        ) as mocked:
            with self.assertRaises(ProviderDataError):
                self.manager._fetch_specific(
                    "test", "breadth", {"timeout": 1}, time.monotonic()
                )
        self.assertEqual(mocked.call_count, 1)

    def test_hard_timeout_is_limited_by_remaining_total_time(self):
        self.manager.network["total_refresh_timeout_seconds"] = 0.05
        seen: list[float] = []

        def fake_timeout(_target, _args, timeout_seconds):
            seen.append(timeout_seconds)
            return {"latency_ms": 1.0, "data": {}}

        with patch(
            "scripts.a_share_panic_index.providers.registry.run_with_hard_timeout",
            side_effect=fake_timeout,
        ):
            self.manager._fetch_specific(
                "test", "breadth", {"timeout": 20}, time.monotonic()
            )
        self.assertLessEqual(seen[0], 0.05)

    def test_primary_data_error_switches_to_backup(self):
        calls: list[str] = []

        def fake_specific(provider, semantic, context, started, max_attempts=None):
            calls.append(provider)
            if provider == "primary":
                raise ProviderDataError("空数组")
            return ({"provider": provider, "data": {}}, [])

        self.manager.priorities["breadth"] = ["primary", "backup"]
        with patch.object(self.manager, "_fetch_specific", side_effect=fake_specific):
            result, _ = self.manager._fetch_chain(
                "breadth", {}, time.monotonic(), allow_missing=False
            )
        self.assertEqual(calls, ["primary", "backup"])
        self.assertEqual(result["provider"], "backup")

    def test_cross_source_disagreement_marks_primary_provisional(self):
        primary = {"provider": "a", "data": {"last": 4000.0}}
        secondary = {"provider": "b", "data": {"last": 3960.0}}
        comparison = self.manager._compare_values(primary, secondary, "index")
        self.assertIsNotNone(comparison)
        self.assertTrue(comparison["exceeds_tolerance"])

    def test_failed_collection_persists_provider_health_without_index_rows(self):
        pipeline = RealtimePipeline(settings(), self.database, test_logger())
        pipeline.providers.last_events = [
            {
                "provider": "failed_source",
                "semantic_type": "index",
                "success": False,
                "error": "连接失败",
            }
        ]
        with patch.object(
            pipeline.providers,
            "collect",
            side_effect=ProviderUnavailable("全部来源失败"),
        ):
            with self.assertRaises(ProviderUnavailable):
                pipeline.run(now(10, 0))
        health = self.database.provider_status()
        self.assertEqual(health[0]["failure_count"], 1)
        self.assertEqual(self.database.realtime_history(), [])

    def test_utf8_daily_log_can_roll_over(self):
        root = Path(self.temp.name) / "中文日志"
        logger = configure_logging(root, 3, "INFO", "测试运行")
        logger.info("中文日志轮转测试")
        handlers = [
            item for item in logger.handlers if hasattr(item, "doRollover")
        ]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertEqual(handler.backupCount, 3)
        handler.doRollover()
        logger.info("轮转后")
        for item in logger.handlers:
            item.flush()
        self.assertTrue((root / "daily.log").exists())
        self.assertTrue(list(root.glob("daily.log.*")))
        for item in logger.handlers:
            item.close()
        logger.handlers.clear()


if __name__ == "__main__":
    unittest.main()
