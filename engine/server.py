"""本地桌面客户端的 A 股恐慌指数服务入口。"""

from __future__ import annotations

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

import argparse
import ctypes
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import uvicorn


API_VERSION = "1"
DEFAULT_CLIENT_VERSION = "2.0.4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地 A 股恐慌指数服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--database", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-directory", required=True)
    parser.add_argument("--client-version", default=DEFAULT_CLIENT_VERSION)
    parser.add_argument("--instance-id", default=None)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--fixture", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-now", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        parser.error("桌面引擎只允许监听 127.0.0.1")
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间")
    if args.parent_pid is not None and args.parent_pid <= 0:
        parser.error("父进程 PID 必须为正整数")
    return args


def _prepare_user_paths(database: str, log_directory: str) -> dict[str, Path]:
    database_path = Path(database).expanduser().resolve()
    log_path = Path(log_directory).expanduser().resolve()
    root = database_path.parent.parent if database_path.parent.name == "data" else log_path.parent
    paths = {
        "root": root,
        "database": database_path,
        "logs": log_path,
        "cache": root / "cache",
        "backups": root / "backups",
        "reports": root / "reports",
    }
    for name, path in paths.items():
        if name == "database":
            path.parent.mkdir(parents=True, exist_ok=True)
            continue
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _start_parent_watcher(parent_pid: int | None) -> None:
    if parent_pid is None:
        return

    def watch_windows() -> None:
        synchronize = 0x00100000
        infinite = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(synchronize, False, parent_pid)
        if not handle:
            os._exit(0)
        try:
            kernel32.WaitForSingleObject(handle, infinite)
        finally:
            kernel32.CloseHandle(handle)
        os._exit(0)

    def watch_posix() -> None:
        while True:
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass
            time.sleep(1)

    target = watch_windows if os.name == "nt" else watch_posix
    threading.Thread(target=target, name="parent-process-watcher", daemon=True).start()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.argv[1:] == ["--worker-self-test"]:
        import json
        sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
        from a_share_panic_index.worker_diagnostics import run_worker_diagnostics
        print(json.dumps(run_worker_diagnostics(), ensure_ascii=False))
        return
    args = parse_args()
    _start_parent_watcher(args.parent_pid)
    paths = _prepare_user_paths(args.database, args.log_directory)
    os.environ["PANIC_INDEX_CACHE_DIRECTORY"] = str(paths["cache"])
    os.environ["MPLCONFIGDIR"] = str(paths["cache"] / "matplotlib")

    script_root = Path(__file__).resolve().parent / "scripts"
    if str(script_root) not in sys.path:
        sys.path.insert(0, str(script_root))

    from a_share_panic_index import APP_VERSION, DB_SCHEMA_VERSION, MODEL_VERSION
    from a_share_panic_index.config import Settings
    from a_share_panic_index.database import Database
    from a_share_panic_index.logging_utils import configure_logging
    from a_share_panic_index.web.app import create_app

    run_id = "desktop-engine"
    fixture_now = datetime.fromisoformat(args.fixture_now) if args.fixture_now else None
    settings = Settings(Path(args.config).expanduser().resolve(), runtime_paths=paths)
    database = Database(paths["database"], paths["backups"])
    logging_config = settings.section("logging")
    logger = configure_logging(
        paths["logs"],
        int(logging_config["retention_days"]),
        str(logging_config["level"]),
        run_id,
    )
    app = create_app(
        settings,
        database,
        logger,
        fixture=args.fixture,
        collector_now=fixture_now,
        start_collector=False,
        runtime={
            "api_version": API_VERSION,
            "app_version": APP_VERSION,
            "engine_version": MODEL_VERSION,
            "database_schema_version": int(DB_SCHEMA_VERSION),
            "client_version": args.client_version,
            "instance_id": args.instance_id or str(uuid4()),
            "paths": {name: str(path) for name, path in paths.items()},
        },
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
