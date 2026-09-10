"""SQLite V5 持久化、迁移和原子写入。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from . import APP_VERSION, DB_SCHEMA_VERSION, MODEL_VERSION
from .models import AggregateSnapshot, DailyResult, RealtimeResult


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata(
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_raw_metrics(
    trade_date TEXT PRIMARY KEY, raw_json TEXT NOT NULL,
    sources_json TEXT NOT NULL, quality_status TEXT NOT NULL,
    collected_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_features(
    trade_date TEXT PRIMARY KEY, feature_values_json TEXT NOT NULL,
    feature_scores_json TEXT NOT NULL, components_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_panic_index(
    trade_date TEXT PRIMARY KEY,
    final_panic_index REAL NOT NULL CHECK(final_panic_index BETWEEN 0 AND 100),
    level TEXT NOT NULL, confidence REAL NOT NULL, coverage REAL NOT NULL,
    finality TEXT NOT NULL CHECK(finality = 'final'),
    quality_status TEXT NOT NULL, components_json TEXT NOT NULL,
    feature_values_json TEXT NOT NULL, feature_scores_json TEXT NOT NULL,
    source_timestamps_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS realtime_raw_metrics(
    trade_date TEXT NOT NULL, timestamp TEXT NOT NULL, bucket_5m INTEGER NOT NULL,
    raw_json TEXT NOT NULL, sources_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(trade_date, timestamp)
);
CREATE TABLE IF NOT EXISTS realtime_features(
    trade_date TEXT NOT NULL, timestamp TEXT NOT NULL, bucket_5m INTEGER NOT NULL,
    reference_mode TEXT NOT NULL, history_days INTEGER NOT NULL,
    historical_blend_weight REAL NOT NULL,
    feature_values_json TEXT NOT NULL, feature_scores_json TEXT NOT NULL,
    components_json TEXT NOT NULL, coverage REAL NOT NULL,
    confidence REAL NOT NULL, quality_status TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(trade_date, timestamp)
);
CREATE TABLE IF NOT EXISTS realtime_panic_index(
    trade_date TEXT NOT NULL, timestamp TEXT NOT NULL, bucket_5m INTEGER NOT NULL,
    realtime_panic_index_raw REAL NOT NULL CHECK(realtime_panic_index_raw BETWEEN 0 AND 100),
    realtime_panic_index REAL NOT NULL CHECK(realtime_panic_index BETWEEN 0 AND 100),
    level TEXT NOT NULL, finality TEXT NOT NULL CHECK(finality = 'provisional'),
    snapshot_type TEXT NOT NULL CHECK(snapshot_type = 'realtime'),
    confidence REAL NOT NULL, coverage REAL NOT NULL,
    reference_mode TEXT NOT NULL, classification_quality TEXT NOT NULL,
    quality_status TEXT NOT NULL, components_json TEXT NOT NULL,
    feature_values_json TEXT NOT NULL, feature_scores_json TEXT NOT NULL,
    feature_contributions_json TEXT NOT NULL, missing_features_json TEXT NOT NULL,
    stale_sources_json TEXT NOT NULL, provisional_reasons_json TEXT NOT NULL,
    source_timestamps_json TEXT NOT NULL, source_skew_seconds REAL NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(trade_date, timestamp)
);
CREATE TABLE IF NOT EXISTS intraday_aggregate_snapshots(
    trade_date TEXT NOT NULL, timestamp TEXT NOT NULL, phase TEXT NOT NULL,
    session_minute INTEGER NOT NULL, bucket_5m INTEGER NOT NULL,
    index_symbol TEXT NOT NULL, index_open REAL NOT NULL,
    index_high REAL NOT NULL, index_low REAL NOT NULL, index_last REAL NOT NULL,
    index_previous_close REAL NOT NULL, up_count INTEGER NOT NULL,
    down_count INTEGER NOT NULL, flat_count INTEGER NOT NULL,
    valid_stock_count INTEGER NOT NULL, decline_share REAL NOT NULL,
    decline_3_share REAL NOT NULL, decline_5_share REAL NOT NULL,
    decline_7_share REAL NOT NULL, median_return REAL NOT NULL,
    limit_up INTEGER NOT NULL, limit_down INTEGER NOT NULL,
    market_amount REAL NOT NULL, incremental_amount_5m REAL,
    projected_full_day_amount REAL, expected_cumulative_share REAL,
    front_contract TEXT, front_price REAL, front_expiry TEXT,
    next_contract TEXT, next_price REAL, next_expiry TEXT,
    qvix_symbol TEXT, qvix REAL, daily_sigma REAL NOT NULL,
    sources_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(trade_date, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_intraday_bucket
ON intraday_aggregate_snapshots(bucket_5m, trade_date, timestamp);
CREATE TABLE IF NOT EXISTS intraday_reference_curves(
    curve_name TEXT NOT NULL, bucket_5m INTEGER NOT NULL,
    cumulative_share REAL NOT NULL, sample_days INTEGER NOT NULL,
    source TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(curve_name, bucket_5m)
);
CREATE TABLE IF NOT EXISTS provider_health(
    provider TEXT NOT NULL, semantic_type TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    latency_ema_ms REAL, health_score REAL NOT NULL DEFAULT 100,
    circuit_open_until TEXT, last_success_at TEXT, last_failure_at TEXT,
    last_error TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, semantic_type)
);
CREATE TABLE IF NOT EXISTS provider_probe_results(
    provider TEXT NOT NULL, semantic_type TEXT NOT NULL,
    result_json TEXT NOT NULL, tested_at TEXT NOT NULL,
    PRIMARY KEY(provider, semantic_type)
);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _load(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


class Database:
    def __init__(self, path: Path, backup_directory: Path | None = None):
        self.path = Path(path).expanduser().resolve()
        self.backup_directory = (
            Path(backup_directory).expanduser().resolve()
            if backup_directory
            else self.path.parent / "backups"
        )
        self.last_backup: Path | None = None
        self._prepare()

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._is_v5():
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = self.backup_directory / f"{self.path.stem}-pre-v5-{stamp}{self.path.suffix}"
            self._backup_before_migration(backup)
            for candidate in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            ):
                candidate.unlink(missing_ok=True)
            self.last_backup = backup
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA_SQL)
            now = datetime.now().astimezone().isoformat()
            for key, value in {
                "schema_version": DB_SCHEMA_VERSION,
                "app_version": APP_VERSION,
                "model_version": MODEL_VERSION,
                "created_at": now,
            }.items():
                self._set_metadata(connection, key, value, now)
            connection.execute(f"PRAGMA user_version={int(DB_SCHEMA_VERSION)}")
            connection.commit()

    def _backup_before_migration(self, backup: Path) -> None:
        """用 SQLite 在线备份保留主库及 WAL 中已提交的数据。"""
        try:
            with closing(sqlite3.connect(self.path)) as source, closing(
                sqlite3.connect(backup)
            ) as destination:
                source.backup(destination)
        except sqlite3.Error:
            backup.unlink(missing_ok=True)
            shutil.copy2(self.path, backup)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(str(backup) + suffix))

    def _is_v5(self) -> bool:
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
                ).fetchone()
                if not exists:
                    return False
                columns = {row[1] for row in connection.execute("PRAGMA table_info(metadata)")}
                if not {"key", "value"}.issubset(columns):
                    return False
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                return bool(row and row[0] == DB_SCHEMA_VERSION)
        except sqlite3.Error:
            return False

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _set_metadata(
        self, connection: sqlite3.Connection, key: str, value: str, updated_at: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, updated_at),
        )

    def write_realtime(
        self,
        aggregate: AggregateSnapshot,
        result: RealtimeResult,
        history_days: int,
        historical_blend_weight: float,
        provider_events: list[dict[str, Any]],
        fail_after_raw: bool = False,
    ) -> None:
        timestamp = aggregate.timestamp.isoformat()
        trade_date = aggregate.trade_date.isoformat()
        created_at = datetime.now().astimezone().isoformat()
        raw = aggregate.to_dict()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO realtime_raw_metrics(
                    trade_date,timestamp,bucket_5m,raw_json,sources_json,created_at
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(trade_date, timestamp) DO UPDATE SET
                    bucket_5m=excluded.bucket_5m, raw_json=excluded.raw_json,
                    sources_json=excluded.sources_json, created_at=excluded.created_at
                """,
                (trade_date, timestamp, aggregate.bucket_5m, _dump(raw),
                 _dump(aggregate.sources), created_at),
            )
            if fail_after_raw:
                raise RuntimeError("测试事务回滚")
            connection.execute(
                """
                INSERT INTO intraday_aggregate_snapshots(
                    trade_date,timestamp,phase,session_minute,bucket_5m,index_symbol,
                    index_open,index_high,index_low,index_last,index_previous_close,
                    up_count,down_count,flat_count,valid_stock_count,decline_share,
                    decline_3_share,decline_5_share,decline_7_share,median_return,
                    limit_up,limit_down,market_amount,incremental_amount_5m,
                    projected_full_day_amount,expected_cumulative_share,
                    front_contract,front_price,front_expiry,next_contract,next_price,
                    next_expiry,qvix_symbol,qvix,daily_sigma,sources_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, timestamp) DO UPDATE SET
                    phase=excluded.phase, session_minute=excluded.session_minute,
                    bucket_5m=excluded.bucket_5m, index_open=excluded.index_open,
                    index_high=excluded.index_high, index_low=excluded.index_low,
                    index_last=excluded.index_last,
                    index_previous_close=excluded.index_previous_close,
                    up_count=excluded.up_count, down_count=excluded.down_count,
                    flat_count=excluded.flat_count,
                    valid_stock_count=excluded.valid_stock_count,
                    decline_share=excluded.decline_share,
                    decline_3_share=excluded.decline_3_share,
                    decline_5_share=excluded.decline_5_share,
                    decline_7_share=excluded.decline_7_share,
                    median_return=excluded.median_return,
                    limit_up=excluded.limit_up, limit_down=excluded.limit_down,
                    market_amount=excluded.market_amount,
                    incremental_amount_5m=excluded.incremental_amount_5m,
                    projected_full_day_amount=excluded.projected_full_day_amount,
                    expected_cumulative_share=excluded.expected_cumulative_share,
                    front_contract=excluded.front_contract, front_price=excluded.front_price,
                    front_expiry=excluded.front_expiry, next_contract=excluded.next_contract,
                    next_price=excluded.next_price, next_expiry=excluded.next_expiry,
                    qvix_symbol=excluded.qvix_symbol, qvix=excluded.qvix,
                    daily_sigma=excluded.daily_sigma, sources_json=excluded.sources_json,
                    created_at=excluded.created_at
                """,
                (
                    trade_date, timestamp, aggregate.phase, aggregate.session_minute,
                    aggregate.bucket_5m, aggregate.index_symbol, aggregate.index_open,
                    aggregate.index_high, aggregate.index_low, aggregate.index_last,
                    aggregate.index_previous_close, aggregate.up_count,
                    aggregate.down_count, aggregate.flat_count,
                    aggregate.valid_stock_count, aggregate.decline_share,
                    aggregate.decline_3_share, aggregate.decline_5_share,
                    aggregate.decline_7_share, aggregate.median_return,
                    aggregate.limit_up, aggregate.limit_down, aggregate.market_amount,
                    aggregate.incremental_amount_5m,
                    aggregate.projected_full_day_amount,
                    aggregate.expected_cumulative_share, aggregate.front_contract,
                    aggregate.front_price,
                    aggregate.front_expiry.isoformat() if aggregate.front_expiry else None,
                    aggregate.next_contract, aggregate.next_price,
                    aggregate.next_expiry.isoformat() if aggregate.next_expiry else None,
                    aggregate.qvix_symbol, aggregate.qvix, aggregate.daily_sigma,
                    _dump(aggregate.sources), created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO realtime_features(
                    trade_date,timestamp,bucket_5m,reference_mode,history_days,
                    historical_blend_weight,feature_values_json,feature_scores_json,
                    components_json,coverage,confidence,quality_status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, timestamp) DO UPDATE SET
                    bucket_5m=excluded.bucket_5m,
                    reference_mode=excluded.reference_mode,
                    history_days=excluded.history_days,
                    historical_blend_weight=excluded.historical_blend_weight,
                    feature_values_json=excluded.feature_values_json,
                    feature_scores_json=excluded.feature_scores_json,
                    components_json=excluded.components_json,
                    coverage=excluded.coverage, confidence=excluded.confidence,
                    quality_status=excluded.quality_status, created_at=excluded.created_at
                """,
                (
                    trade_date, timestamp, aggregate.bucket_5m, result.reference_mode,
                    history_days, historical_blend_weight,
                    _dump(result.feature_values), _dump(result.feature_scores),
                    _dump(result.components), result.coverage, result.confidence,
                    result.quality_status, created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO realtime_panic_index(
                    trade_date,timestamp,bucket_5m,realtime_panic_index_raw,
                    realtime_panic_index,level,finality,snapshot_type,confidence,
                    coverage,reference_mode,classification_quality,quality_status,
                    components_json,feature_values_json,feature_scores_json,
                    feature_contributions_json,missing_features_json,
                    stale_sources_json,provisional_reasons_json,
                    source_timestamps_json,source_skew_seconds,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date, timestamp) DO UPDATE SET
                    bucket_5m=excluded.bucket_5m,
                    realtime_panic_index_raw=excluded.realtime_panic_index_raw,
                    realtime_panic_index=excluded.realtime_panic_index,
                    level=excluded.level, confidence=excluded.confidence,
                    coverage=excluded.coverage, reference_mode=excluded.reference_mode,
                    classification_quality=excluded.classification_quality,
                    quality_status=excluded.quality_status,
                    components_json=excluded.components_json,
                    feature_values_json=excluded.feature_values_json,
                    feature_scores_json=excluded.feature_scores_json,
                    feature_contributions_json=excluded.feature_contributions_json,
                    missing_features_json=excluded.missing_features_json,
                    stale_sources_json=excluded.stale_sources_json,
                    provisional_reasons_json=excluded.provisional_reasons_json,
                    source_timestamps_json=excluded.source_timestamps_json,
                    source_skew_seconds=excluded.source_skew_seconds,
                    created_at=excluded.created_at
                """,
                (
                    trade_date, timestamp, aggregate.bucket_5m,
                    result.realtime_panic_index_raw, result.realtime_panic_index,
                    result.level, result.finality, result.snapshot_type,
                    result.confidence, result.coverage, result.reference_mode,
                    result.classification_quality, result.quality_status,
                    _dump(result.components), _dump(result.feature_values),
                    _dump(result.feature_scores), _dump(result.feature_contributions),
                    _dump(result.missing_features), _dump(result.stale_sources),
                    _dump(result.provisional_reasons), _dump(result.source_timestamps),
                    result.source_skew_seconds, created_at,
                ),
            )
            for event in provider_events:
                self._write_provider_event(connection, event, created_at)
            self._set_metadata(connection, "last_realtime_at", timestamp, created_at)

    def _write_provider_event(
        self,
        connection: sqlite3.Connection,
        event: dict[str, Any],
        updated_at: str,
    ) -> None:
        provider = str(event["provider"])
        semantic_type = str(event["semantic_type"])
        success = bool(event.get("success"))
        existing = connection.execute(
            "SELECT * FROM provider_health WHERE provider=? AND semantic_type=?",
            (provider, semantic_type),
        ).fetchone()
        failures = int(existing["consecutive_failures"]) if existing else 0
        success_count = int(existing["success_count"]) if existing else 0
        failure_count = int(existing["failure_count"]) if existing else 0
        old_latency = existing["latency_ema_ms"] if existing else None
        if success:
            failures = 0
            success_count += 1
            latency = float(event.get("latency_ms", 0))
            latency_ema = latency if old_latency is None else 0.2 * latency + 0.8 * old_latency
            last_success = updated_at
            last_failure = existing["last_failure_at"] if existing else None
            last_error = None
        else:
            failures += 1
            failure_count += 1
            latency_ema = old_latency
            last_success = existing["last_success_at"] if existing else None
            last_failure = updated_at
            last_error = str(event.get("error") or "unknown provider error")
        total = success_count + failure_count
        health_score = 100.0 if total == 0 else 100.0 * success_count / total
        connection.execute(
            """
            INSERT INTO provider_health(
                provider,semantic_type,consecutive_failures,success_count,
                failure_count,latency_ema_ms,health_score,circuit_open_until,
                last_success_at,last_failure_at,last_error,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider, semantic_type) DO UPDATE SET
                consecutive_failures=excluded.consecutive_failures,
                success_count=excluded.success_count, failure_count=excluded.failure_count,
                latency_ema_ms=excluded.latency_ema_ms, health_score=excluded.health_score,
                circuit_open_until=excluded.circuit_open_until,
                last_success_at=excluded.last_success_at,
                last_failure_at=excluded.last_failure_at,
                last_error=excluded.last_error, updated_at=excluded.updated_at
            """,
            (
                provider, semantic_type, failures, success_count, failure_count,
                latency_ema, health_score, event.get("circuit_open_until"),
                last_success, last_failure, last_error, updated_at,
            ),
        )

    def record_provider_events(self, events: list[dict[str, Any]]) -> None:
        """指数事务失败时，独立保存本轮数据源健康事件。"""
        if not events:
            return
        updated_at = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            for event in events:
                self._write_provider_event(connection, event, updated_at)

    def write_daily(
        self,
        raw: dict[str, Any],
        result: DailyResult,
        fail_after_raw: bool = False,
    ) -> None:
        trade_date = result.trade_date.isoformat()
        now = datetime.now().astimezone().isoformat()
        sources = raw.get("sources", {})
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO daily_raw_metrics(
                    trade_date,raw_json,sources_json,quality_status,collected_at,updated_at
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    raw_json=excluded.raw_json, sources_json=excluded.sources_json,
                    quality_status=excluded.quality_status,
                    collected_at=excluded.collected_at, updated_at=excluded.updated_at
                """,
                (trade_date, _dump(raw), _dump(sources), result.quality_status, now, now),
            )
            if fail_after_raw:
                raise RuntimeError("测试事务回滚")
            connection.execute(
                """
                INSERT INTO daily_features(
                    trade_date,feature_values_json,feature_scores_json,
                    components_json,updated_at
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    feature_values_json=excluded.feature_values_json,
                    feature_scores_json=excluded.feature_scores_json,
                    components_json=excluded.components_json,
                    updated_at=excluded.updated_at
                """,
                (trade_date, _dump(result.feature_values), _dump(result.feature_scores),
                 _dump(result.components), now),
            )
            connection.execute(
                """
                INSERT INTO daily_panic_index(
                    trade_date,final_panic_index,level,confidence,coverage,finality,
                    quality_status,components_json,feature_values_json,
                    feature_scores_json,source_timestamps_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    final_panic_index=excluded.final_panic_index,
                    level=excluded.level, confidence=excluded.confidence,
                    coverage=excluded.coverage, quality_status=excluded.quality_status,
                    components_json=excluded.components_json,
                    feature_values_json=excluded.feature_values_json,
                    feature_scores_json=excluded.feature_scores_json,
                    source_timestamps_json=excluded.source_timestamps_json,
                    updated_at=excluded.updated_at
                """,
                (
                    trade_date, result.final_panic_index, result.level,
                    result.confidence, result.coverage, result.finality,
                    result.quality_status, _dump(result.components),
                    _dump(result.feature_values), _dump(result.feature_scores),
                    _dump(result.source_timestamps), now, now,
                ),
            )
            self._set_metadata(connection, "last_final_date", trade_date, now)

    def latest_realtime(self, before: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM realtime_panic_index"
        params: tuple[Any, ...] = ()
        if before:
            query += " WHERE timestamp <= ?"
            params = (before,)
        query += " ORDER BY timestamp DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return self._decode_realtime(row) if row else None

    def latest_realtime_with_aggregate(self) -> dict[str, Any] | None:
        value = self.latest_realtime()
        if value is None:
            return None
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM intraday_aggregate_snapshots
                WHERE trade_date=? AND timestamp=? LIMIT 1
                """,
                (value["trade_date"], value["timestamp"]),
            ).fetchone()
        if row:
            aggregate = dict(row)
            aggregate["sources"] = _load(aggregate.pop("sources_json"), {})
            value["aggregate"] = aggregate
        else:
            value["aggregate"] = None
        return value

    def realtime_history(
        self,
        trade_date: date | str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM realtime_panic_index"
        if trade_date:
            value = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
            query += " WHERE trade_date=? ORDER BY timestamp DESC LIMIT ?"
            params: tuple[Any, ...] = (value, limit)
        else:
            query += " ORDER BY timestamp DESC LIMIT ?"
            params = (limit,)
        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_realtime(row) for row in reversed(rows)]

    @staticmethod
    def _decode_realtime(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in (
            "components_json", "feature_values_json", "feature_scores_json",
            "feature_contributions_json", "missing_features_json",
            "stale_sources_json", "provisional_reasons_json",
            "source_timestamps_json",
        ):
            value[key.removesuffix("_json")] = _load(value.pop(key), {})
        return value

    def latest_daily(self, before_date: date | str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM daily_panic_index"
        params: tuple[Any, ...] = ()
        if before_date:
            value = before_date.isoformat() if isinstance(before_date, date) else before_date
            query += " WHERE trade_date <= ?"
            params = (value,)
        query += " ORDER BY trade_date DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return self._decode_daily(row) if row else None

    def daily_history(
        self, start: date | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM daily_panic_index"
        if start:
            query += " WHERE trade_date >= ? ORDER BY trade_date DESC LIMIT ?"
            params: tuple[Any, ...] = (start.isoformat(), limit)
        else:
            query += " ORDER BY trade_date DESC LIMIT ?"
            params = (limit,)
        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_daily(row) for row in reversed(rows)]

    @staticmethod
    def _decode_daily(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in (
            "components_json", "feature_values_json", "feature_scores_json",
            "source_timestamps_json",
        ):
            value[key.removesuffix("_json")] = _load(value.pop(key), {})
        return value

    def current_day_aggregates(self, trade_date: date) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM intraday_aggregate_snapshots WHERE trade_date=? ORDER BY timestamp",
                (trade_date.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_aggregate(self, trade_date: date | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM intraday_aggregate_snapshots"
        params: tuple[Any, ...] = ()
        if trade_date:
            query += " WHERE trade_date=?"
            params = (trade_date.isoformat(),)
        query += " ORDER BY timestamp DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def latest_closing_aggregate(self, trade_date: date) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM intraday_aggregate_snapshots
                WHERE trade_date=? AND session_minute=241 AND bucket_5m=48
                ORDER BY timestamp DESC LIMIT 1
                """,
                (trade_date.isoformat(),),
            ).fetchone()
        return dict(row) if row else None

    def realtime_at(self, trade_date: date, timestamp: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM realtime_panic_index
                WHERE trade_date=? AND timestamp=? LIMIT 1
                """,
                (trade_date.isoformat(), timestamp),
            ).fetchone()
        return self._decode_realtime(row) if row else None

    def daily_raw_history(
        self, before_date: date, limit: int = 800
    ) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT raw_json FROM daily_raw_metrics
                WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?
                """,
                (before_date.isoformat(), limit),
            ).fetchall()
        return list(reversed([_load(row[0], {}) for row in rows]))

    def daily_feature_history(
        self, before_date: date, limit: int = 800
    ) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT trade_date,feature_values_json,feature_scores_json
                FROM daily_features WHERE trade_date < ?
                ORDER BY trade_date DESC LIMIT ?
                """,
                (before_date.isoformat(), limit),
            ).fetchall()
        return list(
            reversed(
                [
                    {
                        "trade_date": row["trade_date"],
                        "feature_values": _load(row["feature_values_json"], {}),
                        "feature_scores": _load(row["feature_scores_json"], {}),
                    }
                    for row in rows
                ]
            )
        )

    def aggregate_curve_rows(self, limit_days: int = 250) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT trade_date,bucket_5m,MAX(market_amount) AS market_amount
                FROM intraday_aggregate_snapshots
                WHERE trade_date IN (
                    SELECT DISTINCT trade_date FROM intraday_aggregate_snapshots
                    ORDER BY trade_date DESC LIMIT ?
                )
                GROUP BY trade_date,bucket_5m
                ORDER BY trade_date,bucket_5m
                """,
                (limit_days,),
            ).fetchall()
        return [dict(row) for row in rows]

    def closing_snapshot_dates(self) -> list[date]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT trade_date FROM intraday_aggregate_snapshots
                WHERE session_minute=241 AND bucket_5m=48
                ORDER BY trade_date
                """
            ).fetchall()
        return [date.fromisoformat(row[0]) for row in rows]

    def previous_bucket_aggregate(
        self, trade_date: date, bucket_5m: int
    ) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM intraday_aggregate_snapshots
                WHERE trade_date=? AND bucket_5m <= ?
                ORDER BY bucket_5m DESC, timestamp DESC LIMIT 1
                """,
                (trade_date.isoformat(), max(bucket_5m - 1, 0)),
            ).fetchone()
        return dict(row) if row else None

    def same_bucket_history(
        self, bucket_5m: int, before_date: date, limit: int = 250
    ) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT trade_date, feature_values_json, feature_scores_json
                FROM realtime_features
                WHERE bucket_5m=? AND trade_date < ?
                AND timestamp IN (
                    SELECT MAX(timestamp) FROM realtime_features
                    WHERE bucket_5m=? AND trade_date < ? GROUP BY trade_date
                )
                ORDER BY trade_date DESC LIMIT ?
                """,
                (bucket_5m, before_date.isoformat(), bucket_5m,
                 before_date.isoformat(), limit),
            ).fetchall()
        return [
            {
                "trade_date": row["trade_date"],
                "feature_values": _load(row["feature_values_json"], {}),
                "feature_scores": _load(row["feature_scores_json"], {}),
            }
            for row in rows
        ]

    def upsert_reference_curve(
        self,
        curve_name: str,
        values: dict[int, float],
        sample_days: int,
        source: str,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.transaction() as connection:
            for bucket, share in values.items():
                connection.execute(
                    """
                    INSERT INTO intraday_reference_curves(
                        curve_name,bucket_5m,cumulative_share,sample_days,source,updated_at
                    ) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(curve_name, bucket_5m) DO UPDATE SET
                        cumulative_share=excluded.cumulative_share,
                        sample_days=excluded.sample_days, source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (curve_name, bucket, share, sample_days, source, now),
                )

    def reference_curve(
        self, bucket_5m: int, curve_name: str | None = None
    ) -> dict[str, Any] | None:
        where = "WHERE bucket_5m=?"
        params: tuple[Any, ...] = (bucket_5m,)
        if curve_name:
            where += " AND curve_name=?"
            params = (bucket_5m, curve_name)
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"""
                SELECT * FROM intraday_reference_curves
                {where} ORDER BY sample_days DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return dict(row) if row else None

    def reference_curves(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM intraday_reference_curves ORDER BY curve_name,bucket_5m"
            ).fetchall()
        return [dict(row) for row in rows]

    def realtime_validation_rows(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.trade_date,r.timestamp,r.realtime_panic_index_raw,
                       r.realtime_panic_index,a.index_last
                FROM realtime_panic_index r
                JOIN intraday_aggregate_snapshots a
                  ON a.trade_date=r.trade_date AND a.timestamp=r.timestamp
                ORDER BY r.timestamp
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_validation_rows(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT d.trade_date,d.final_panic_index,r.raw_json
                FROM daily_panic_index d JOIN daily_raw_metrics r
                  ON r.trade_date=d.trade_date ORDER BY d.trade_date
                """
            ).fetchall()
        output = []
        for row in rows:
            raw = _load(row["raw_json"], {})
            output.append(
                {
                    "trade_date": row["trade_date"],
                    "final_panic_index": row["final_panic_index"],
                    "close": raw.get("close"),
                }
            )
        return output

    def provider_status(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM provider_health ORDER BY semantic_type, provider"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_probe_results(self, results: list[dict[str, Any]]) -> None:
        with self.transaction() as connection:
            for result in results:
                connection.execute(
                    """
                    INSERT INTO provider_probe_results(
                        provider,semantic_type,result_json,tested_at
                    ) VALUES (?,?,?,?)
                    ON CONFLICT(provider, semantic_type) DO UPDATE SET
                        result_json=excluded.result_json, tested_at=excluded.tested_at
                    """,
                    (result["provider"], result["semantic_type"],
                     _dump(result), result["tested_at"]),
                )

    def probe_results(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT result_json FROM provider_probe_results ORDER BY semantic_type, provider"
            ).fetchall()
        return [_load(row[0], {}) for row in rows]

    def journal_mode(self) -> str:
        with closing(self.connect()) as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
