"""V3 HTTP API和单页Dashboard。"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, time
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .. import DB_SCHEMA_VERSION, MODEL_VERSION
from ..calendar import TradingCalendar
from ..chart import ChartError, generate_chart
from ..pipeline.daily import DailyPipeline
from ..pipeline.realtime import RealtimePipeline, PipelineError
from ..providers.base import ProviderError
from ..pipeline.historical import HistoricalService


STATIC_ROOT = Path(__file__).resolve().parent / "static"


class RealtimeCollector:
    def __init__(
        self,
        settings,
        database,
        logger,
        fixture: str | None = None,
        fixed_now: datetime | None = None,
    ):
        self.settings = settings
        self.database = database
        self.logger = logger
        self.fixture = fixture
        self.fixed_now = fixed_now
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="panic-index-collector", daemon=True
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=5)

    def collect_once(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"status": "collector_busy"}
        try:
            current = self.current_time()
            result, meta = RealtimePipeline(
                self.settings, self.database, self.logger
            ).run(current, fixture=self.fixture)
            payload = result.to_dict() if result else meta
            daily = self._finalize_if_ready(current)
            if daily is not None:
                payload["daily"] = daily
            return payload
        finally:
            self._lock.release()

    def current_time(self) -> datetime:
        timezone = ZoneInfo(self.settings.get("market.timezone"))
        current = self.fixed_now or datetime.now(timezone)
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone)
        return current.astimezone(timezone)

    def _finalize_if_ready(self, current: datetime) -> dict[str, Any] | None:
        finalization = self.settings.get("market.finalization_time", "15:10")
        hour, minute = (int(part) for part in str(finalization).split(":"))
        if current.time().replace(tzinfo=None) < time(hour, minute):
            return None
        market = self.settings.section("market")
        calendar = TradingCalendar(
            market.get("calendar", "XSHG"), market.get("timezone", "Asia/Shanghai")
        )
        trade_date = current.date()
        if not calendar.is_session(trade_date):
            return None
        existing = self.database.latest_daily(trade_date)
        if existing and existing["trade_date"] == trade_date.isoformat():
            return existing
        aggregate = self.database.latest_closing_aggregate(trade_date)
        if not aggregate or not self.database.realtime_at(trade_date, aggregate["timestamp"]):
            return None
        result, _ = DailyPipeline(
            self.settings, self.database, self.logger
        ).run(current, requested_date=trade_date)
        return result.to_dict() if result else None

    def _loop(self) -> None:
        interval = int(self.settings.get("realtime.refresh_seconds"))
        while not self._stop.is_set():
            try:
                self.collect_once()
            except Exception as error:
                self.logger.exception("Dashboard实时采集失败: %s", error)
            self._stop.wait(interval)


def create_app(
    settings,
    database,
    logger,
    fixture: str | None = None,
    start_collector: bool = False,
    runtime: dict[str, Any] | None = None,
    collector_now: datetime | None = None,
) -> FastAPI:
    runtime = runtime or {}
    runtime_paths = runtime.get("paths") or {}
    reports_directory = Path(
        runtime_paths.get("reports", database.path.parent.parent / "reports")
    ).expanduser().resolve()
    reports_directory.mkdir(parents=True, exist_ok=True)
    collector = RealtimeCollector(settings, database, logger, fixture, collector_now)
    historical = HistoricalService(settings, database, logger)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_collector:
            collector.start()
        try:
            yield
        finally:
            collector.stop()

    app = FastAPI(title="A股实时恐慌指数", version="3.0", lifespan=lifespan)
    app.state.collector = collector

    @app.get("/api/v1/history")
    def historical_estimates() -> dict[str, Any]:
        return historical.read()

    @app.post("/api/v1/history/refresh")
    def historical_refresh() -> dict[str, Any]:
        try:
            return historical.refresh(collector.current_time().date())
        except (ProviderError, PipelineError) as error:
            raise HTTPException(status_code=503, detail={"code":"history_failed", "message":str(error), "retry_after_seconds":60}) from error

    @app.post("/api/v1/realtime/refresh")
    def realtime_refresh() -> dict[str, Any]:
        try:
            value = collector.collect_once()
        except (ProviderError, PipelineError) as error:
            logger.exception("行情采集失败: %s", error)
            raise HTTPException(status_code=503, detail={
                "code": "collection_failed",
                "message": str(error),
                "retry_after_seconds": 60,
            }) from error
        if value.get("status") == "collector_busy":
            raise HTTPException(status_code=409, detail="实时采集正在进行")
        return value

    @app.post("/api/v1/chart")
    def chart(
        chart_type: str = Query(alias="type", pattern="^(intraday|daily)$"),
    ) -> dict[str, str]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output = reports_directory / f"panic-{chart_type}-{stamp}-{uuid4().hex[:8]}.png"
        try:
            generate_chart(
                database,
                output,
                chart_type=chart_type,
                trade_date=(
                    collector.current_time().date()
                    if chart_type == "intraday"
                    else None
                ),
                as_of_date=collector.current_time().date(),
            )
        except ChartError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"path": str(output)}

    @app.get("/api/v1/realtime")
    def realtime() -> dict[str, Any]:
        value = database.latest_realtime_with_aggregate()
        if value is None:
            raise HTTPException(status_code=404, detail="暂无盘中数据")
        return value

    @app.get("/api/v1/realtime/history")
    def realtime_history(
        trade_date: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> list[dict[str, Any]]:
        return database.realtime_history(trade_date, limit)

    @app.get("/api/v1/daily/latest")
    def daily_latest() -> dict[str, Any]:
        value = database.latest_daily()
        if value is None:
            raise HTTPException(status_code=404, detail="暂无收盘正式数据")
        return value

    @app.get("/api/v1/daily/history")
    def daily_history(
        limit: int = Query(500, ge=1, le=5000),
    ) -> list[dict[str, Any]]:
        return database.daily_history(limit=limit)

    @app.get("/api/v1/sources")
    def sources() -> dict[str, Any]:
        return {
            "health": database.provider_status(),
            "probe": database.probe_results(),
        }

    @app.get("/api/v1/reference")
    def reference() -> dict[str, Any]:
        latest = database.latest_realtime()
        return {
            "current_reference_mode": latest.get("reference_mode") if latest else None,
            "curves": database.reference_curves(),
        }

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "api_version": runtime.get("api_version", "1"),
            "engine_version": runtime.get("engine_version", MODEL_VERSION),
            "database_schema_version": int(
                runtime.get("database_schema_version", DB_SCHEMA_VERSION)
            ),
            "client_version": runtime.get("client_version", "2.0.0"),
            "instance_id": runtime.get("instance_id"),
            "pid": os.getpid(),
            "database_journal_mode": database.journal_mode(),
            "collector_running": bool(
                collector._thread and collector._thread.is_alive()
            ),
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        path = STATIC_ROOT / "index.html"
        return HTMLResponse(path.read_text(encoding="utf-8"))

    return app
