"""全市场宽度聚合。"""

from __future__ import annotations

from math import log
from typing import Iterable

import numpy as np


def aggregate_returns(change_percent: Iterable[float]) -> dict[str, float | int]:
    values = np.asarray(list(change_percent), dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError("全市场涨跌幅为空")
    valid = int(values.size)
    up = int(np.sum(values > 0))
    down = int(np.sum(values < 0))
    flat = valid - up - down
    return {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "valid_stock_count": valid,
        "decline_share": down / valid,
        "decline_3_share": float(np.sum(values <= -0.03) / valid),
        "decline_5_share": float(np.sum(values <= -0.05) / valid),
        "decline_7_share": float(np.sum(values <= -0.07) / valid),
        "median_return": float(np.median(values)),
    }


def breadth_feature_values(
    down_count: int,
    valid_stock_count: int,
    decline_5_share: float,
    decline_7_share: float,
    median_return: float,
    limit_up: int,
    limit_down: int,
) -> dict[str, float]:
    if valid_stock_count <= 0:
        raise ValueError("有效股票数必须大于0")
    decline_share = down_count / valid_stock_count
    limit_down_share = max(limit_down, 0) / valid_stock_count
    return {
        "decline_share": decline_share,
        "severe_decline_share": max(0.0, float(decline_5_share)),
        "extreme_decline_share": max(0.0, float(decline_7_share)),
        "median_return_stress": -float(median_return),
        "limit_down_intensity": log(1.0 + 1000.0 * limit_down_share),
        "limit_imbalance": log((max(limit_down, 0) + 1.0) / (max(limit_up, 0) + 1.0)),
    }
