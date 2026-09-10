"""盘中实时特征构建，只使用当前及过去数据。"""

from __future__ import annotations

from math import log, sqrt
from typing import Any

import numpy as np

from ..models import AggregateSnapshot
from .breadth import breadth_feature_values
from .derivatives import annualized_basis
from .liquidity import liquidity_feature_values


def build_realtime_feature_values(
    aggregate: AggregateSnapshot,
    current_day_history: list[dict[str, Any]],
    previous_bucket: dict[str, Any] | None,
    annualization_days: int = 365,
) -> dict[str, float | None]:
    epsilon = 1e-9
    daily_sigma = max(float(aggregate.daily_sigma), epsilon)
    progress = max(aggregate.session_minute / 241.0, 1.0 / 241.0)
    gap_return = log(aggregate.index_open / aggregate.index_previous_close)
    intraday_return = log(aggregate.index_last / aggregate.index_previous_close)
    prices = [float(row["index_last"]) for row in current_day_history]
    if not prices or prices[-1] != aggregate.index_last:
        prices.append(float(aggregate.index_last))
    minute_returns = np.diff(np.log(np.asarray(prices, dtype=float)))
    realized = float(sqrt(float(np.square(minute_returns).sum()))) if minute_returns.size else 0.0
    downside = (
        float(sqrt(float(np.square(np.minimum(minute_returns, 0)).sum())))
        if minute_returns.size
        else 0.0
    )
    history_highs = [float(row["index_high"]) for row in current_day_history]
    history_lows = [float(row["index_low"]) for row in current_day_history]
    high_so_far = max(history_highs + [aggregate.index_high])
    low_so_far = min(history_lows + [aggregate.index_low])
    range_so_far = log(high_so_far / low_so_far)
    previous_price = _value(previous_bucket, "index_last")
    return_5m = (
        log(aggregate.index_last / previous_price)
        if previous_price is not None and previous_price > 0
        else None
    )
    values: dict[str, float | None] = {
        "gap_down_z": max(0.0, -gap_return) / daily_sigma,
        "decline_from_previous_close_z": -intraday_return
        / max(daily_sigma * sqrt(progress), epsilon),
        "realized_vol_so_far_z": realized
        / max(daily_sigma * sqrt(progress), epsilon),
        "downside_vol_so_far_z": downside
        / max(daily_sigma * sqrt(progress), epsilon),
        "range_so_far_z": range_so_far
        / max(daily_sigma * sqrt(progress), epsilon),
        "down_shock_5m_z": (
            max(0.0, -return_5m) / max(daily_sigma * sqrt(5.0 / 241.0), epsilon)
            if return_5m is not None
            else None
        ),
    }
    values.update(
        breadth_feature_values(
            aggregate.down_count,
            aggregate.valid_stock_count,
            aggregate.decline_5_share,
            aggregate.decline_7_share,
            aggregate.median_return,
            aggregate.limit_up,
            aggregate.limit_down,
        )
    )
    front_basis = _basis(
        aggregate.index_last,
        aggregate.front_price,
        aggregate.front_expiry,
        aggregate.trade_date,
        annualization_days,
    )
    next_basis = _basis(
        aggregate.index_last,
        aggregate.next_price,
        aggregate.next_expiry,
        aggregate.trade_date,
        annualization_days,
    )
    previous_front_basis = None
    if previous_bucket and aggregate.front_expiry:
        previous_front_basis = _basis(
            float(previous_bucket["index_last"]),
            _value(previous_bucket, "front_price"),
            aggregate.front_expiry,
            aggregate.trade_date,
            annualization_days,
        )
    values.update(
        {
            "front_annualized_basis": front_basis,
            "next_annualized_basis": next_basis,
            "basis_curve_stress": (
                front_basis - next_basis
                if front_basis is not None and next_basis is not None
                else None
            ),
            "basis_widening_5m": (
                front_basis - previous_front_basis
                if front_basis is not None and previous_front_basis is not None
                else None
            ),
            "qvix_level": aggregate.qvix,
            "qvix_change_from_previous_close": (
                log(aggregate.qvix / aggregate.qvix_previous_close)
                if aggregate.qvix and aggregate.qvix_previous_close
                else None
            ),
            "qvix_change_5m": (
                log(
                    aggregate.qvix
                    / float(
                        aggregate.qvix_previous_5m
                        or previous_bucket["qvix"]
                    )
                )
                if aggregate.qvix
                and (
                    aggregate.qvix_previous_5m
                    or (previous_bucket and _value(previous_bucket, "qvix"))
                )
                else None
            ),
        }
    )
    expected_incremental = _expected_incremental(aggregate, previous_bucket)
    previous_incremental = _previous_increment(current_day_history)
    values.update(
        liquidity_feature_values(
            aggregate.projected_full_day_amount,
            aggregate.median_daily_market_amount_20,
            return_5m,
            aggregate.incremental_amount_5m,
            expected_incremental,
            previous_incremental,
        )
    )
    return values


def _basis(
    spot: float,
    futures_price: float | None,
    expiry,
    trade_date,
    annualization_days: int,
) -> float | None:
    if futures_price is None or expiry is None:
        return None
    days = (expiry - trade_date).days
    if days <= 0:
        return None
    return annualized_basis(spot, futures_price, days, annualization_days)


def _value(row: dict[str, Any] | None, key: str) -> float | None:
    if not row or row.get(key) is None:
        return None
    return float(row[key])


def _expected_incremental(
    aggregate: AggregateSnapshot,
    previous_bucket: dict[str, Any] | None,
) -> float | None:
    if (
        aggregate.median_daily_market_amount_20 is None
        or aggregate.expected_cumulative_share is None
    ):
        return None
    previous_share = _value(previous_bucket, "expected_cumulative_share") or 0.0
    share_delta = aggregate.expected_cumulative_share - previous_share
    if share_delta <= 0:
        return None
    return aggregate.median_daily_market_amount_20 * share_delta


def _previous_increment(history: list[dict[str, Any]]) -> float | None:
    values = [
        float(row["incremental_amount_5m"])
        for row in history
        if row.get("incremental_amount_5m") is not None
        and float(row["incremental_amount_5m"]) >= 0
    ]
    return values[-1] if values else None
