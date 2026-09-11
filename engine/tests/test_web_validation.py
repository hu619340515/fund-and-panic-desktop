from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import tempfile
import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.a_share_panic_index.pipeline.daily import DailyPipeline
from scripts.a_share_panic_index.pipeline.realtime import RealtimePipeline
from scripts.a_share_panic_index.providers.base import ProviderUnavailable
from scripts.a_share_panic_index.validation import run_validation
from scripts.a_share_panic_index.web import create_app
from tests.helpers import REALTIME_FIXTURE, make_database, now, settings, test_logger


class TestWebAndValidation(unittest.TestCase):
    def test_collection_failure_keeps_health_and_daily_available(self):
        app = create_app(self.settings, self.database, self.logger)
        with TestClient(app) as client, patch.object(
            app.state.collector, "collect_once",
            side_effect=ProviderUnavailable("breadth全部数据源失败: sina: 超时"),
        ):
            response = client.post("/api/v1/realtime/refresh")
            self.assertEqual(response.status_code, 503)
            detail = response.json()["detail"]
            self.assertEqual(detail["code"], "collection_failed")
            self.assertIn("sina", detail["message"])
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/api/v1/daily/latest").status_code, 200)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = make_database(self.root)
        self.settings = settings()
        self.logger = test_logger()
        realtime = RealtimePipeline(self.settings, self.database, self.logger)
        realtime.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
        realtime.run(now(10, 5), fixture=str(REALTIME_FIXTURE))
        realtime.run(now(15, 10), fixture=str(REALTIME_FIXTURE))
        DailyPipeline(self.settings, self.database, self.logger).run(
            now(16, 0), requested_date=now(16, 0).date()
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_api_contract_and_dashboard(self):
        app = create_app(
            self.settings, self.database, self.logger, start_collector=False
        )
        with TestClient(app) as client:
            realtime = client.get("/api/v1/realtime")
            self.assertEqual(realtime.status_code, 200)
            payload = realtime.json()
            self.assertIn("realtime_panic_index_raw", payload)
            self.assertIn("confidence", payload)
            self.assertIn("reference_mode", payload)
            self.assertEqual(payload["aggregate"]["up_count"], 850)
            self.assertEqual(payload["aggregate"]["bucket_5m"], 48)
            self.assertEqual(client.get("/api/v1/realtime/history").status_code, 200)
            self.assertEqual(client.get("/api/v1/daily/latest").status_code, 200)
            self.assertEqual(client.get("/api/v1/daily/history").status_code, 200)
            self.assertEqual(client.get("/api/v1/sources").status_code, 200)
            self.assertEqual(client.get("/api/v1/reference").status_code, 200)
            health = client.get("/healthz").json()
            self.assertEqual(health["database_journal_mode"], "wal")
            dashboard = client.get("/")
            self.assertIn("盘中实时估计，不是收盘正式值", dashboard.text)

    def test_desktop_refresh_chart_and_health_contract(self):
        reports = self.root / "用户数据" / "reports"
        database = make_database(self.root / "桌面运行")
        app = create_app(
            self.settings,
            database,
            self.logger,
            fixture=str(REALTIME_FIXTURE),
            start_collector=False,
            collector_now=now(15, 10),
            runtime={
                "engine_version": "3.0-realtime",
                "database_schema_version": 5,
                "client_version": "2.0.0-test",
                "instance_id": "instance-test",
                "paths": {"reports": str(reports)},
            },
        )
        with TestClient(app) as client:
            refreshed = client.post("/api/v1/realtime/refresh")
            self.assertEqual(refreshed.status_code, 200)
            self.assertIn("realtime_panic_index", refreshed.json())
            self.assertIn("daily", refreshed.json())
            self.assertEqual(client.get("/api/v1/daily/latest").status_code, 200)
            for chart_type in ("intraday", "daily"):
                response = client.post(f"/api/v1/chart?type={chart_type}")
                self.assertEqual(response.status_code, 200)
                path = Path(response.json()["path"])
                self.assertEqual(path.parent, reports.resolve())
                self.assertTrue(path.exists())
            health = client.get("/healthz").json()
            self.assertTrue(health["ok"])
            self.assertEqual(health["api_version"], "1")
            self.assertEqual(health["engine_version"], "3.0-realtime")
            self.assertEqual(health["database_schema_version"], 5)
            self.assertEqual(health["client_version"], "2.0.0-test")
            self.assertEqual(health["instance_id"], "instance-test")
            self.assertEqual(health["pid"], os.getpid())

    def test_collector_is_singleton(self):
        app = create_app(
            self.settings,
            self.database,
            self.logger,
            fixture=str(REALTIME_FIXTURE),
            start_collector=False,
        )
        collector = app.state.collector
        self.assertTrue(collector.start())
        self.assertFalse(collector.start())
        collector.stop()

    def test_validation_uses_only_stored_history(self):
        result = run_validation(self.database, "realtime", self.root / "验证")
        self.assertEqual(result["validation_status"], "insufficient_intraday_history")
        self.assertEqual(result["data_policy"], "stored_intraday_snapshots_only")
        self.assertTrue(Path(result["json_output"]).exists())
        header = Path(result["csv_output"]).read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("验证模式", header)

    def test_api_reads_while_realtime_writer_upserts(self):
        app = create_app(
            self.settings, self.database, self.logger, start_collector=False
        )

        def write_once():
            return RealtimePipeline(
                self.settings, self.database, self.logger
            ).run(now(15, 10), fixture=str(REALTIME_FIXTURE))[0]

        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(write_once)
            statuses = [client.get("/api/v1/realtime").status_code for _ in range(10)]
            self.assertIsNotNone(future.result(timeout=10))
        self.assertEqual(statuses, [200] * 10)


if __name__ == "__main__":
    unittest.main()
