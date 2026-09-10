from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import PROJECT_ROOT, PROBE_FIXTURE, REALTIME_FIXTURE


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="CLI中文路径-")
        self.root = Path(self.temp.name)
        self.config = self.root / "配置.yaml"
        self.config.write_text(
            """
database:
  path: ./数据/指数.db
  backup_directory: ./数据/备份
logging:
  directory: ./日志
  retention_days: 3
  level: INFO
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(
        self,
        arguments: list[str],
        now: str = "2026-07-24T10:00:00+08:00",
        root_entry: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        entry = PROJECT_ROOT / ("cli.py" if root_entry else "scripts/cli.py")
        command = [sys.executable, str(entry), *arguments]
        environment = os.environ.copy()
        environment["PANIC_INDEX_NOW"] = now
        environment["PYTHONUTF8"] = "1"
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

    def common(self) -> list[str]:
        return ["--config", str(self.config)]

    def test_realtime_stdout_is_single_json_and_logs_go_to_stderr(self):
        completed = self.run_cli(
            ["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], "3.0")
        self.assertEqual(payload["result"]["snapshot_type"], "realtime")
        self.assertIn("realtime开始", completed.stderr)

    def test_watch_outputs_one_json_per_iteration(self):
        completed = self.run_cli(
            [
                "realtime",
                *self.common(),
                "--fixture",
                str(REALTIME_FIXTURE),
                "--watch",
                "--interval",
                "30",
                "--iterations",
                "2",
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(json.loads(line)["ok"] for line in lines))

    def test_non_trading_day_returns_latest_without_new_date(self):
        self.run_cli(["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)])
        completed = self.run_cli(
            ["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)],
            now="2026-07-25T10:00:00+08:00",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "skipped_non_trading_day")
        self.assertEqual(payload["as_of_date"], "2026-07-24")

    def test_stale_and_incomplete_have_fixed_exit_codes(self):
        payload = json.loads((REALTIME_FIXTURE / "snapshot.json").read_text(encoding="utf-8"))
        payload["providers"]["index"]["source_timestamp"] = "2026-07-24T09:50:00+08:00"
        stale = self.root / "stale.json"
        stale.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        completed = self.run_cli(["realtime", *self.common(), "--fixture", str(stale)])
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(json.loads(completed.stdout)["status"], "stale")
        payload = json.loads((REALTIME_FIXTURE / "snapshot.json").read_text(encoding="utf-8"))
        payload["providers"]["futures"]["data"]["contracts"] = []
        incomplete = self.root / "incomplete.json"
        incomplete.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        completed = self.run_cli(
            ["realtime", *self.common(), "--fixture", str(incomplete)]
        )
        self.assertEqual(completed.returncode, 4)
        self.assertEqual(json.loads(completed.stdout)["status"], "incomplete")

    def test_daily_current_chart_replay_and_validation(self):
        self.run_cli(["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)])
        second = self.run_cli(
            ["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)],
            now="2026-07-24T10:05:00+08:00",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        closing = self.run_cli(
            ["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)],
            now="2026-07-24T15:10:00+08:00",
        )
        self.assertEqual(closing.returncode, 0, closing.stderr)
        daily = self.run_cli(
            ["daily", *self.common(), "--date", "2026-07-24"],
            now="2026-07-24T16:00:00+08:00",
        )
        self.assertEqual(daily.returncode, 0, daily.stderr)
        daily_payload = json.loads(daily.stdout)
        self.assertEqual(daily_payload["result"]["finality"], "final")

    def test_daily_rejects_morning_snapshot_as_final(self):
        self.run_cli(["realtime", *self.common(), "--fixture", str(REALTIME_FIXTURE)])
        daily = self.run_cli(
            [
                "daily",
                *self.common(),
                "--date",
                "2026-07-24",
                "--fixture",
                str(REALTIME_FIXTURE / "snapshot.json"),
            ],
            now="2026-07-24T16:00:00+08:00",
        )
        self.assertEqual(daily.returncode, 3)
        self.assertIsNone(json.loads(daily.stdout)["result"])
        current = self.run_cli(["current", *self.common()])
        self.assertEqual(current.returncode, 0, current.stderr)
        image = self.root / "报告" / "盘中.png"
        chart = self.run_cli(
            ["chart", *self.common(), "--type", "intraday", "--output", str(image)]
        )
        self.assertEqual(chart.returncode, 0, chart.stderr)
        self.assertTrue(image.exists())
        replay = self.run_cli(
            ["replay", *self.common(), "--date", "2026-07-24", "--speed", "20"]
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_payload = json.loads(replay.stdout)
        self.assertFalse(replay_payload["result"]["network_access"])
        validation = self.run_cli(
            ["validate", *self.common(), "--mode", "realtime", "--output", str(self.root / "验证")]
        )
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertEqual(
            json.loads(validation.stdout)["result"]["validation_status"],
            "insufficient_intraday_history",
        )

    def test_source_probe_fixture_and_root_entry(self):
        output = self.root / "探测" / "source_probe.json"
        completed = self.run_cli(
            [
                "sources",
                "probe",
                *self.common(),
                "--fixture",
                str(PROBE_FIXTURE),
                "--output",
                str(output),
            ],
            root_entry=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(output.exists())
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["result"]["probe_mode"], "fixture")

    def test_importing_cli_is_lazy(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import scripts.cli; print('matplotlib' in sys.modules, 'fastapi' in sys.modules)",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
