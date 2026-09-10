"""收盘正式模型特征。"""

from __future__ import annotations

from math import log, sqrt
from typing import Any

import numpy as np
import pandas as pd

from .breadth import breadth_feature_values


def build_daily_feature_values(
    current: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, float | None]:
    rows = history + [current]
    frame = pd.DataFrame(rows)
    required = {"open", "high", "low", "close", "previous_close", "market_amount"}
    if not required.issubset(frame.columns):
        raise ValueError("收盘原始数据缺少价格或成交额字段")
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[list(required)].isna().any().any():
        raise ValueError("收盘原始数据包含空值")
    returns = np.log(frame["close"] / frame["previous_close"])
    negative = np.minimum(returns, 0.0)
    ewma = (
        returns.ewm(span=5, adjust=False).std().iloc[-1] * sqrt(252)
        if len(returns) >= 5
        else None
    )
    realized20 = returns.tail(20).std(ddof=1) * sqrt(252) if len(returns) >= 20 else None
    downside20 = (
        float(sqrt(float(np.square(negative.tail(20)).mean())) * sqrt(252))
        if len(returns) >= 20
        else None
    )
    parkinson = np.log(frame["high"] / frame["low"]) ** 2
    parkinson10 = (
        float(
            sqrt(float(parkinson.tail(10).mean()) / (4.0 * log(2.0)))
            * sqrt(252)
        )
        if len(parkinson) >= 10
        else None
    )
    jump = max(0.0, -float(returns.iloc[-1]))
    values: dict[str, float | None] = {
        "ewma_volatility_5": (
            float(ewma) if ewma is not None and np.isfinite(ewma) else None
        ),
        "realized_volatility_20": (
            float(realized20)
            if realized20 is not None and np.isfinite(realized20)
            else None
        ),
        "downside_volatility_20": downside20,
        "parkinson_volatility_10": parkinson10,
        "daily_down_jump": jump,
    }
    values.update(
        breadth_feature_values(
            int(current["down_count"]),
            int(current["valid_stock_count"]),
            float(current["decline_5_share"]),
            float(current["decline_7_share"]),
            float(current["median_return"]),
            int(current["limit_up"]),
            int(current["limit_down"]),
        )
    )
    values.update(
        {
            "front_annualized_basis": current.get("front_annualized_basis"),
            "next_annualized_basis": current.get("next_annualized_basis"),
            "basis_curve_stress": current.get("basis_curve_stress"),
            "basis_expansion_3d": current.get("basis_expansion_3d"),
            "qvix_level": current.get("qvix"),
            "qvix_daily_change": current.get("qvix_daily_change"),
        }
    )
    amounts = frame["market_amount"].iloc[:-1].tail(20)
    median_amount = float(amounts.median()) if not amounts.empty else None
    amount_ratio = (
        float(current["market_amount"]) / median_amount
        if median_amount and median_amount > 0
        else None
    )
    market_return = float(returns.iloc[-1])
    values.update(
        {
            "daily_amount_shortfall": -log(amount_ratio) if amount_ratio and amount_ratio > 0 else None,
            "daily_amihud": abs(market_return) / max(float(current["market_amount"]) / 1e8, 1e-9),
            "daily_downside_turnover": max(0.0, -market_return)
            * max(0.0, log(amount_ratio))
            if amount_ratio and amount_ratio > 0
            else None,
        }
    )
    return values
