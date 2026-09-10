"""为图表 E2E 创建隔离的 SQLite 测试夹具。

本脚本只应写入传入的测试用户目录数据库。数值来自
engine/tests/fixtures/realtime 的现有 fixture，不能作为生产行情或交付数据库使用。
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "engine"
sys.path.insert(0, str(ENGINE_ROOT / "scripts"))

from a_share_panic_index.database import Database
from a_share_panic_index.models import AggregateSnapshot, DailyResult, RealtimeResult


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXTURE = ENGINE_ROOT / "tests" / "fixtures" / "realtime"


def fixture_snapshot(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def source_values(snapshot: dict) -> tuple[dict, dict, dict, dict, dict, dict]:
    providers = snapshot["providers"]
    return (
        providers["index"]["data"],
        providers["breadth"]["data"],
        providers["limits"]["data"],
        providers["futures"]["data"],
        providers["qvix"]["data"],
        providers["daily_baseline"]["data"],
    )


def make_realtime(snapshot: dict, trade_date: date, hour: int, minute: int, score: float) -> tuple[AggregateSnapshot, RealtimeResult]:
    index, breadth, limits, futures, qvix, baseline = source_values(snapshot)
    timestamp = datetime(
        trade_date.year, trade_date.month, trade_date.day, hour, minute, tzinfo=SHANGHAI
    )
    contracts = futures["contracts"]
    front, next_contract = contracts[0], contracts[1]
    bucket = (hour - 9) * 12 + minute // 5
    aggregate = AggregateSnapshot(
        trade_date=trade_date,
        timestamp=timestamp,
        phase="continuous",
        session_minute=max(1, (hour - 9) * 60 + minute - 30),
        bucket_5m=bucket,
        index_symbol=str(index["symbol"]),
        index_open=float(index["open"]),
        index_high=float(index["high"]),
        index_low=float(index["low"]),
        index_last=float(index["last"]),
        index_previous_close=float(index["previous_close"]),
        index_volume=float(index["volume"]),
        index_amount=float(index["amount"]),
        up_count=int(breadth["up_count"]),
        down_count=int(breadth["down_count"]),
        flat_count=int(breadth["flat_count"]),
        valid_stock_count=int(breadth["valid_stock_count"]),
        decline_share=float(breadth["decline_share"]),
        decline_3_share=float(breadth["decline_3_share"]),
        decline_5_share=float(breadth["decline_5_share"]),
        decline_7_share=float(breadth["decline_7_share"]),
        median_return=float(breadth["median_return"]),
        limit_up=int(limits["limit_up"]),
        limit_down=int(limits["limit_down"]),
        market_amount=float(breadth["market_amount"]),
        incremental_amount_5m=20_000_000_000.0,
        projected_full_day_amount=1_100_000_000_000.0,
        expected_cumulative_share=0.16,
        front_contract=str(front["symbol"]),
        front_price=float(front["last"]),
        front_bid=float(front["bid"]),
        front_ask=float(front["ask"]),
        front_expiry=date.fromisoformat(front["expiry"]),
        next_contract=str(next_contract["symbol"]),
        next_price=float(next_contract["last"]),
        next_bid=float(next_contract["bid"]),
        next_ask=float(next_contract["ask"]),
        next_expiry=date.fromisoformat(next_contract["expiry"]),
        qvix_symbol=str(qvix["symbol"]),
        qvix=float(qvix["value"]),
        qvix_previous_close=float(qvix["previous_close"]),
        qvix_previous_5m=float(qvix["previous_close"]),
        daily_sigma=float(baseline["daily_sigma"]),
        median_daily_market_amount_20=float(baseline["median_daily_market_amount_20"]),
        sources=snapshot["providers"],
        provisional_reasons=["测试夹具：现有离线 fixture"],
    )
    components = {
        "volatility": min(100.0, score + 4.0),
        "breadth": min(100.0, score + 8.0),
        "derivatives": max(0.0, score - 5.0),
        "liquidity": max(0.0, score - 2.0),
    }
    source_timestamps = {
        name: str(value["source_timestamp"])
        for name, value in snapshot["providers"].items()
    }
    result = RealtimeResult(
        trade_date=trade_date,
        timestamp=timestamp,
        bucket_5m=bucket,
        realtime_panic_index_raw=min(100.0, score + 1.75),
        realtime_panic_index=score,
        level="偏恐慌" if score >= 60 else "中性",
        components=components,
        feature_values={"fixture_score": score},
        feature_scores={"fixture_score": score},
        feature_contributions={"fixture_score": score},
        confidence=88.0,
        coverage=1.0,
        reference_mode="fixture_e2e",
        classification_quality="fixture",
        quality_status="complete",
        missing_features=[],
        stale_sources=[],
        provisional_reasons=["测试夹具：现有离线 fixture"],
        source_timestamps=source_timestamps,
        source_skew_seconds=0.0,
    )
    return aggregate, result


def make_daily_raw(snapshot: dict, trade_date: date) -> dict:
    index, breadth, limits, futures, qvix, baseline = source_values(snapshot)
    return {
        "fixture_notice": "仅图表 E2E 测试；源自 engine/tests/fixtures/realtime，不是生产行情",
        "trade_date": trade_date.isoformat(),
        "open": index["open"],
        "high": index["high"],
        "low": index["low"],
        "close": index["last"],
        "previous_close": index["previous_close"],
        "up_count": breadth["up_count"],
        "down_count": breadth["down_count"],
        "flat_count": breadth["flat_count"],
        "valid_stock_count": breadth["valid_stock_count"],
        "decline_share": breadth["decline_share"],
        "decline_5_share": breadth["decline_5_share"],
        "decline_7_share": breadth["decline_7_share"],
        "median_return": breadth["median_return"],
        "limit_up": limits["limit_up"],
        "limit_down": limits["limit_down"],
        "market_amount": breadth["market_amount"],
        "daily_sigma": baseline["daily_sigma"],
        "front_contract": futures["contracts"][0]["symbol"],
        "qvix": qvix["value"],
        "sources": snapshot["providers"],
    }


def seed(database_path: Path) -> dict:
    database = Database(database_path, database_path.parent / "backups")
    today = datetime.now(SHANGHAI).date()
    first = fixture_snapshot("snapshot.json")
    second = fixture_snapshot("1005.json")

    # 当前上海日期用于让引擎的 intraday chart 选择到本测试数据；另放一条前日记录验证日期过滤。
    for snapshot, hour, minute, score in (
        (first, 9, 55, 54.0),
        (first, 10, 0, 58.0),
        (second, 10, 5, 67.0),
        (first, 10, 0, 49.0),
    ):
        target_date = today if score != 49.0 else today - timedelta(days=1)
        aggregate, result = make_realtime(snapshot, target_date, hour, minute, score)
        database.write_realtime(aggregate, result, 0, 0.0, [])

    # 只写三条相隔很远的收盘记录，故意留下缺口，验证图表不会补齐日期。
    daily_dates = (today - timedelta(days=350), today - timedelta(days=167), today - timedelta(days=9))
    for offset, trade_date in enumerate(daily_dates):
        score = (41.0, 63.0, 52.0)[offset]
        database.write_daily(
            make_daily_raw(first if offset != 1 else second, trade_date),
            DailyResult(
                trade_date=trade_date,
                final_panic_index=score,
                level="偏恐慌" if score >= 60 else "中性",
                components={"volatility": score, "breadth": score, "derivatives": score, "liquidity": score},
                feature_values={"fixture_score": score},
                feature_scores={"fixture_score": score},
                confidence=90.0,
                coverage=1.0,
                quality_status="complete",
                source_timestamps={"fixture": "engine/tests/fixtures/realtime"},
            ),
        )
    return {
        "fixture": "engine/tests/fixtures/realtime（仅测试，不是生产行情）",
        "database": str(database.path),
        "trade_date": today.isoformat(),
        "intraday_current_records": len(database.realtime_history(today)),
        "intraday_all_records": len(database.realtime_history()),
        "daily_records": len(database.daily_history(limit=5000)),
        "daily_dates": [item["trade_date"] for item in database.daily_history(limit=5000)],
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法: seed-chart-fixture.py <测试数据库路径>")
    database_path = Path(sys.argv[1]).expanduser().resolve()
    if database_path.name != "panic-index.db":
        raise SystemExit("测试数据库文件名必须为 panic-index.db")
    print(json.dumps(seed(database_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
