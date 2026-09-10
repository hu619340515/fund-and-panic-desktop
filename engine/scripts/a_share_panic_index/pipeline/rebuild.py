"""正式日线历史重建，不生成虚构记录。"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ..features.daily import build_daily_feature_values
from .daily import DailyPipeline


class RebuildPipeline:
    def __init__(self, settings, database, logger):
        self.settings = settings
        self.database = database
        self.logger = logger

    def run(self, fixture: str | None = None) -> dict[str, Any]:
        if fixture:
            return self._from_fixture(Path(fixture))
        dates = self.database.closing_snapshot_dates()
        if not dates:
            return {
                "status": "no_real_history",
                "rebuilt_days": 0,
                "data_policy": "no_fabrication",
                "network_access": False,
                "available_closing_snapshot_days": 0,
            }
        pipeline = DailyPipeline(self.settings, self.database, self.logger)
        completed = []
        errors = []
        timezone = pipeline.calendar.timezone
        for trade_date in dates:
            try:
                result, _ = pipeline.run(
                    datetime.combine(trade_date, time(16, 0), tzinfo=timezone),
                    requested_date=trade_date,
                )
                if result:
                    completed.append(trade_date.isoformat())
            except Exception as error:
                errors.append({"trade_date": trade_date.isoformat(), "error": str(error)})
        return {
            "status": "rebuild_complete" if completed else "no_rebuildable_history",
            "rebuilt_days": len(completed),
            "dates": completed,
            "errors": errors,
            "data_policy": "stored_snapshots_only_no_fill",
            "network_access": False,
            "available_closing_snapshot_days": len(dates),
            "available_start_date": dates[0].isoformat(),
            "available_end_date": dates[-1].isoformat(),
        }

    def _from_fixture(self, fixture: Path) -> dict[str, Any]:
        target = fixture / "daily_history.json" if fixture.is_dir() else fixture
        if not target.exists():
            raise FileNotFoundError(f"日线重建夹具不存在: {target}")
        with target.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        records = payload.get("records", payload)
        if not isinstance(records, list) or not records:
            raise ValueError("日线重建夹具必须包含非空records数组")
        records = sorted(records, key=lambda item: item["trade_date"])
        pipeline = DailyPipeline(self.settings, self.database, self.logger)
        completed = []
        for raw in records:
            trade_date = date.fromisoformat(raw["trade_date"])
            history = self.database.daily_raw_history(trade_date)
            values = build_daily_feature_values(raw, history)
            result = pipeline._score(trade_date, raw, values)
            self.database.write_daily(raw, result)
            completed.append(trade_date.isoformat())
        return {
            "status": "rebuild_complete",
            "rebuilt_days": len(completed),
            "dates": completed,
            "source": str(target.resolve()),
            "data_policy": "fixture_records_only_no_fill",
            "network_access": False,
            "available_start_date": completed[0],
            "available_end_date": completed[-1],
        }
