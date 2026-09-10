"""结构化命令日志。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_logging(directory: Path, retention_days: int, level: str, run_id: str) -> logging.Logger:
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("a_share_panic_index")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    formatter = logging.Formatter(
        f"%(asctime)s %(levelname)s run_id={run_id} %(name)s %(message)s"
    )
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        directory / "daily.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger
