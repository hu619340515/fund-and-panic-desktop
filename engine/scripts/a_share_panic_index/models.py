"""跨数据源、计算和存储层使用的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


def jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


@dataclass(slots=True)
class MarketContext:
    now: datetime
    requested_date: date
    expected_trade_date: date
    is_trading_day: bool
    phase: str
    session_minute: int | None
    bucket_5m: int | None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class ProviderResult:
    provider: str
    semantic_type: str
    data: dict[str, Any]
    source_timestamp: datetime
    requested_at: datetime
    received_at: datetime
    provisional: bool = False
    quality_flags: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class ProbeResult:
    provider: str
    endpoint_or_function: str
    semantic_type: str
    available: bool
    latency_ms: float
    returned_rows: int
    earliest_timestamp: str | None
    latest_timestamp: str | None
    fields: list[str]
    units: dict[str, str]
    source_timestamp: str | None
    supports_realtime: bool
    supports_1m: bool
    supports_5m: bool
    supports_history: bool
    maximum_observed_rows: int
    requires_cookie: bool
    requires_login: bool
    error: str | None
    tested_at: str

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class AggregateSnapshot:
    trade_date: date
    timestamp: datetime
    phase: str
    session_minute: int
    bucket_5m: int
    index_symbol: str
    index_open: float
    index_high: float
    index_low: float
    index_last: float
    index_previous_close: float
    index_volume: float | None
    index_amount: float | None
    up_count: int
    down_count: int
    flat_count: int
    valid_stock_count: int
    decline_share: float
    decline_3_share: float
    decline_5_share: float
    decline_7_share: float
    median_return: float
    limit_up: int
    limit_down: int
    market_amount: float
    incremental_amount_5m: float | None
    projected_full_day_amount: float | None
    expected_cumulative_share: float | None
    front_contract: str | None
    front_price: float | None
    front_bid: float | None
    front_ask: float | None
    front_expiry: date | None
    next_contract: str | None
    next_price: float | None
    next_bid: float | None
    next_ask: float | None
    next_expiry: date | None
    qvix_symbol: str | None
    qvix: float | None
    qvix_previous_close: float | None
    qvix_previous_5m: float | None
    daily_sigma: float
    median_daily_market_amount_20: float | None
    sources: dict[str, dict[str, Any]]
    provisional_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class RealtimeResult:
    trade_date: date
    timestamp: datetime
    bucket_5m: int
    realtime_panic_index_raw: float
    realtime_panic_index: float
    level: str
    components: dict[str, float]
    feature_values: dict[str, float | None]
    feature_scores: dict[str, float | None]
    feature_contributions: dict[str, float]
    confidence: float
    coverage: float
    reference_mode: str
    classification_quality: str
    quality_status: str
    missing_features: list[str]
    stale_sources: list[str]
    provisional_reasons: list[str]
    source_timestamps: dict[str, str]
    source_skew_seconds: float
    finality: str = "provisional"
    snapshot_type: str = "realtime"

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class DailyResult:
    trade_date: date
    final_panic_index: float
    level: str
    components: dict[str, float]
    feature_values: dict[str, float | None]
    feature_scores: dict[str, float | None]
    confidence: float
    coverage: float
    quality_status: str
    source_timestamps: dict[str, str]
    finality: str = "final"
    snapshot_type: str = "daily"

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))
