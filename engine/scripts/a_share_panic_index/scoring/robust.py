"""只使用历史样本的稳健参考模式。"""

from __future__ import annotations

from math import isfinite
from typing import Iterable


def historical_percentile(value: float, history: Iterable[float]) -> float | None:
    samples = sorted(float(item) for item in history if isfinite(float(item)))
    if not samples:
        return None
    below = sum(item < value for item in samples)
    equal = sum(item == value for item in samples)
    return 100.0 * (below + 0.5 * equal) / len(samples)


def reference_state(history_days: int, config: dict) -> tuple[str, float]:
    start = int(config["self_calibration_start_days"])
    same_time = int(config["same_time_history_days"])
    cap = float(config.get("feature_historical_blend_cap", 0.50))
    if history_days < start:
        return "structural_bootstrap", 0.0
    if history_days < same_time:
        span = max(same_time - start, 1)
        return "self_calibrating", cap * (history_days - start) / span
    return "same_time_history", cap


def blend_with_history(
    structural_score: float,
    current_value: float,
    historical_values: Iterable[float],
    historical_weight: float,
) -> float:
    historical_score = historical_percentile(current_value, historical_values)
    if historical_score is None or historical_weight <= 0:
        return max(0.0, min(100.0, float(structural_score)))
    weight = max(0.0, min(0.5, float(historical_weight)))
    return max(
        0.0,
        min(100.0, (1.0 - weight) * structural_score + weight * historical_score),
    )
