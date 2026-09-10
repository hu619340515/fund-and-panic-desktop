"""盘中成交额曲线和流动性特征。"""

from __future__ import annotations

from math import isfinite, log
from statistics import median
from typing import Any


def bootstrap_cumulative_share(bucket_5m: int) -> float:
    """仅在真实代理曲线不可用时使用，并必须标记bootstrap。"""
    bucket = max(0, min(48, int(bucket_5m)))
    if bucket <= 24:
        progress = (bucket + 1) / 25.0
        return min(0.56, 0.56 * progress ** 0.78)
    progress = (bucket - 24) / 24.0
    return min(1.0, 0.56 + 0.44 * progress ** 0.86)


def combine_proxy_curves(curves: list[dict[int, float]]) -> dict[int, float]:
    """按时间桶取多个真实代理品种的中位数。"""
    if not curves:
        return {}
    combined: dict[int, float] = {}
    previous = 0.0
    for bucket in range(49):
        values = [curve[bucket] for curve in curves if bucket in curve]
        if not values:
            continue
        value = float(median(values))
        if not isfinite(value) or value <= 0 or value > 1.000001:
            raise ValueError(f"代理成交曲线桶{bucket}数值无效")
        value = max(previous, min(1.0, value))
        combined[bucket] = value
        previous = value
    if 48 not in combined or combined[48] < 0.999:
        raise ValueError("代理成交曲线缺少完整收盘桶")
    combined[48] = 1.0
    return combined


def curves_from_amount_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把真实5分钟成交额行转换为逐品种历史中位累计比例。"""
    grouped: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        trade_date = str(row.get("trade_date") or "").strip()
        try:
            bucket = int(row["bucket_5m"])
            amount = float(row["amount"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("代理成交额行字段缺失或无效") from error
        if not symbol or not trade_date or not 0 <= bucket <= 48:
            raise ValueError("代理成交额行代码、日期或时间桶无效")
        if not isfinite(amount) or amount < 0:
            raise ValueError("代理成交额必须是有限非负数")
        grouped.setdefault(symbol, {}).setdefault(trade_date, []).append((bucket, amount))
    output: list[dict[str, Any]] = []
    for symbol, days in grouped.items():
        shares_by_bucket: dict[int, list[float]] = {}
        valid_days = 0
        for values in days.values():
            ordered = sorted(values)
            total = sum(amount for _, amount in ordered)
            if total <= 0 or not any(bucket == 48 for bucket, _ in ordered):
                continue
            valid_days += 1
            cumulative = 0.0
            daily: dict[int, float] = {}
            for bucket, amount in ordered:
                cumulative += amount
                daily[bucket] = cumulative / total
            first_share = daily[min(daily)]
            daily.setdefault(0, first_share)
            daily[48] = 1.0
            for bucket, share in daily.items():
                shares_by_bucket.setdefault(bucket, []).append(share)
        if valid_days:
            curve = {
                bucket: float(median(values))
                for bucket, values in shares_by_bucket.items()
                if values
            }
            output.append(
                {"symbol": symbol, "sample_days": valid_days, "curve": curve}
            )
    return output


def blended_cumulative_share(
    proxy_share: float,
    self_collected_share: float | None,
    history_days: int,
) -> tuple[float, str, float]:
    if self_collected_share is None or history_days < 20:
        return proxy_share, "proxy_curve", 0.0
    if history_days < 60:
        weight = 0.5 * (history_days - 20) / 40.0
    elif history_days < 120:
        weight = 0.5 + 0.25 * (history_days - 60) / 60.0
    else:
        weight = 0.75
    share = (1.0 - weight) * proxy_share + weight * self_collected_share
    return max(1e-6, min(1.0, share)), "blended_curve", weight


def liquidity_feature_values(
    projected_full_day_amount: float | None,
    median_daily_market_amount_20: float | None,
    market_return_5m: float | None,
    incremental_amount_5m: float | None,
    expected_incremental_amount: float | None,
    previous_incremental_amount: float | None,
) -> dict[str, float | None]:
    projected_shortfall = None
    if projected_full_day_amount and median_daily_market_amount_20:
        ratio = projected_full_day_amount / median_daily_market_amount_20
        if ratio > 0:
            projected_shortfall = -log(max(ratio, 1e-9))
    illiquidity = None
    downside_shock = None
    acceleration_stress = None
    if incremental_amount_5m is not None:
        if incremental_amount_5m < 0:
            raise ValueError("最近5分钟成交额增量不能为负")
        if market_return_5m is not None and incremental_amount_5m > 0:
            illiquidity = abs(market_return_5m) / max(incremental_amount_5m / 1e8, 1e-9)
            if expected_incremental_amount and expected_incremental_amount > 0:
                downside_shock = max(0.0, -market_return_5m) * max(
                    0.0, log(incremental_amount_5m / expected_incremental_amount)
                )
            if previous_incremental_amount and previous_incremental_amount > 0:
                acceleration = incremental_amount_5m / previous_incremental_amount
                acceleration_stress = max(0.0, log(acceleration)) * max(
                    0.0, -market_return_5m
                )
    return {
        "projected_amount_shortfall": projected_shortfall,
        "incremental_5m_illiquidity": illiquidity,
        "downside_turnover_shock": downside_shock,
        "amount_acceleration_stress": acceleration_stress,
    }
