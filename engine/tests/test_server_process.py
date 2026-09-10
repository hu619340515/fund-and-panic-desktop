from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from tests.helpers import PROJECT_ROOT, REALTIME_FIXTURE, now


def available_port() -> int:
    with closing(socket.socket()) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def request_json(url: str, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


class TestServerProcess(unittest.TestCase):
    def test_offline_child_process_chinese_paths_health_refresh_and_charts(self):
        with tempfile.TemporaryDirectory(prefix="桌面引擎中文路径-") as directory:
            user_data = Path(directory) / "用户数据"
            database_path = user_data / "data" / "panic-index.db"
            config_path = user_data / "config" / "panic.yaml"
            log_directory = user_data / "logs"
            config_path.parent.mkdir(parents=True)
            shutil.copy2(PROJECT_ROOT / "config" / "settings.yaml", config_path)

            port = available_port()
            instance_id = "中文实例-001"
            command = [
                sys.executable,
                str(PROJECT_ROOT / "server.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database",
                str(database_path),
                "--config",
                str(config_path),
                "--log-directory",
                str(log_directory),
                "--client-version",
                "2.0.0-test",
                "--instance-id",
                instance_id,
                "--fixture",
                str(REALTIME_FIXTURE),
                "--fixture-now",
                now(15, 10).isoformat(),
            ]
            process = subprocess.Popen(
                command,
                cwd=user_data,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            base_url = f"http://127.0.0.1:{port}"
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        self.fail(f"引擎子进程提前退出: {process.returncode}")
                    try:
                        health = request_json(base_url + "/healthz")
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            self.fail("引擎子进程健康检查超时")
                        time.sleep(0.1)

                self.assertTrue(health["ok"])
                self.assertEqual(health["api_version"], "1")
                self.assertEqual(health["engine_version"], "3.0-realtime")
                self.assertEqual(health["database_schema_version"], 5)
                self.assertEqual(health["client_version"], "2.0.0-test")
                self.assertEqual(health["instance_id"], instance_id)
                self.assertEqual(health["pid"], process.pid)

                refreshed = request_json(base_url + "/api/v1/realtime/refresh", "POST")
                self.assertIn("realtime_panic_index", refreshed)
                self.assertIn("daily", refreshed)
                for chart_type in ("intraday", "daily"):
                    chart = request_json(
                        base_url + f"/api/v1/chart?type={chart_type}", "POST"
                    )
                    chart_path = Path(chart["path"])
                    self.assertEqual(chart_path.parent, (user_data / "reports").resolve())
                    self.assertTrue(chart_path.exists())

                for name in ("cache", "reports", "backups"):
                    self.assertTrue((user_data / name).is_dir())
                with closing(sqlite3.connect(database_path)) as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM realtime_panic_index"
                    ).fetchone()[0]
                self.assertGreaterEqual(count, 1)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    @unittest.skipUnless(os.name == "nt", "Windows 父进程句柄测试")
    def test_engine_exits_after_parent_process_ends(self):
        import ctypes

        with tempfile.TemporaryDirectory(prefix="父进程退出-") as directory:
            user_data = Path(directory) / "用户数据"
            database_path = user_data / "data" / "panic-index.db"
            config_path = user_data / "config" / "panic.yaml"
            log_directory = user_data / "logs"
            pid_path = user_data / "引擎.pid"
            config_path.parent.mkdir(parents=True)
            shutil.copy2(PROJECT_ROOT / "config" / "settings.yaml", config_path)
            port = available_port()
            server_command = [
                sys.executable,
                str(PROJECT_ROOT / "server.py"),
                "--port",
                str(port),
                "--database",
                str(database_path),
                "--config",
                str(config_path),
                "--log-directory",
                str(log_directory),
                "--parent-pid",
                "PARENT_PID",
            ]
            helper_code = """
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
command = json.loads(sys.argv[1])
command[command.index('PARENT_PID')] = str(os.getpid())
child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 30
while True:
    if child.poll() is not None:
        raise SystemExit(2)
    try:
        with urllib.request.urlopen(sys.argv[2] + '/healthz', timeout=2) as response:
            health = json.loads(response.read().decode('utf-8'))
        Path(sys.argv[3]).write_text(str(health['pid']), encoding='utf-8')
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise SystemExit(3)
        time.sleep(0.1)
"""
            helper = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    helper_code,
                    json.dumps(server_command, ensure_ascii=False),
                    f"http://127.0.0.1:{port}",
                    str(pid_path),
                ],
                cwd=user_data,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            engine_pid = None
            try:
                self.assertEqual(helper.wait(timeout=40), 0)
                engine_pid = int(pid_path.read_text(encoding="utf-8"))
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.restype = ctypes.c_void_p
                handle = kernel32.OpenProcess(0x00100000, False, engine_pid)
                if handle:
                    try:
                        self.assertEqual(kernel32.WaitForSingleObject(handle, 10_000), 0)
                    finally:
                        kernel32.CloseHandle(handle)
                with self.assertRaises(OSError):
                    request_json(f"http://127.0.0.1:{port}/healthz")
            finally:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait(timeout=10)
                if engine_pid is not None:
                    try:
                        os.kill(engine_pid, signal.SIGTERM)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
