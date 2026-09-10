from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.a_share_panic_index.config import Settings
from scripts.a_share_panic_index.database import Database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REALTIME_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "realtime"
NO_QVIX_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "realtime_no_qvix"
PROBE_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "source_probe"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_logger() -> logging.Logger:
    logger = logging.getLogger("panic-index-tests")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def make_database(directory: Path, name: str = "panic.db") -> Database:
    return Database(directory / name, directory / "backups")


def now(hour: int, minute: int = 0, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=SHANGHAI)


def settings() -> Settings:
    return Settings()
