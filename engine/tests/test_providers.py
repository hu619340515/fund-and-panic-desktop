from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import requests

from scripts.a_share_panic_index.config import Settings
from scripts.a_share_panic_index.providers.base import (
    HttpClient,
    ProviderDataError,
    ProviderTimeout,
    run_with_hard_timeout,
)
from scripts.a_share_panic_index.providers.probe import run_source_probe
from scripts.a_share_panic_index.providers.registry import ProviderManager
from tests.helpers import PROBE_FIXTURE, SHANGHAI, make_database, test_logger


class TestProviders(unittest.TestCase):
    def test_hard_timeout_terminates_worker(self):
        with self.assertRaises(ProviderTimeout):
            run_with_hard_timeout(time.sleep, (2,), 0.05)

    def test_http_403_and_html_error_page_are_rejected(self):
        client = HttpClient(1)
        forbidden = Mock(status_code=403, headers={}, text="forbidden")
        forbidden.raise_for_status.side_effect = requests.HTTPError("403")
        client.session.get = Mock(return_value=forbidden)
        with self.assertRaises(ProviderDataError):
            client.get("https://example.invalid")
        html = Mock(
            status_code=200,
            headers={"Content-Type": "text/html"},
            text="<html>gateway error</html>",
        )
        html.raise_for_status.return_value = None
        client.session.get = Mock(return_value=html)
        with self.assertRaises(ProviderDataError):
            client.get("https://example.invalid")

    def test_limits_can_be_explicitly_estimated_from_breadth(self):
        breadth = {
            "data": {
                "change_percent_values": [0.10, 0.11, -0.10, -0.11, 0.01]
            }
        }
        result, events = ProviderManager._estimate_limits(
            breadth, {"now": "2026-07-24T10:00:00+08:00"}
        )
        self.assertEqual(result["data"], {"limit_up": 2, "limit_down": 2})
        self.assertTrue(result["provisional"])
        self.assertIn("limits_estimated_from_breadth", result["quality_flags"])
        self.assertTrue(events[0]["success"])

    def test_fixture_probe_writes_json_and_chinese_csv_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = make_database(root)
            output = root / "报告" / "source_probe.json"
            result = run_source_probe(
                Settings(), database, output, fixture=PROBE_FIXTURE
            )
            self.assertEqual(result["probe_mode"], "fixture")
            self.assertGreaterEqual(len(result["results"]), 8)
            header = Path(result["coverage_csv"]).read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("数据源", header)
            self.assertIn("金融语义", header)
            self.assertTrue(database.probe_results())

    def test_circuit_opens_after_repeated_failures_and_recovers_after_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_database(Path(directory))
            timestamp = datetime.now(SHANGHAI).isoformat()
            with database.transaction() as connection:
                for _ in range(3):
                    database._write_provider_event(
                        connection,
                        {
                            "provider": "test_source",
                            "semantic_type": "index",
                            "success": False,
                            "error": "timeout",
                        },
                        timestamp,
                    )
            manager = ProviderManager(Settings(), database, test_logger())
            self.assertTrue(manager._circuit_open("test_source", "index"))
            old = (datetime.now(SHANGHAI) - timedelta(hours=1)).isoformat()
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE provider_health SET last_failure_at=? WHERE provider='test_source'",
                    (old,),
                )
            manager = ProviderManager(Settings(), database, test_logger())
            self.assertFalse(manager._circuit_open("test_source", "index"))

    def test_unknown_configuration_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text("unknown_section:\n  value: 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "未知配置键"):
                Settings(path)


if __name__ == "__main__":
    unittest.main()
