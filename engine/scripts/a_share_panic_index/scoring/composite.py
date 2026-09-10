"""组件聚合和显示平滑。"""

from __future__ import annotations

from math import isfinite
from typing import Mapping


def weighted_available_score(
    scores: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> tuple[float | None, float]:
    total_weight = sum(float(value) for value in weights.values())
    available = {
        name: float(scores[name])
        for name in weights
        if scores.get(name) is not None and isfinite(float(scores[name]))
    }
    available_weight = sum(float(weights[name]) for name in available)
    coverage = 0.0 if total_weight <= 0 else available_weight / total_weight
    if not available or available_weight <= 0:
        return None, coverage
    score = sum(available[name] * float(weights[name]) for name in available)
    return score / available_weight, coverage


def generalized_mean(
    components: Mapping[str, float | None],
    weights: Mapping[str, float],
    power: float = 1.5,
    minimum_weight: float = 0.80,
) -> tuple[float | None, float]:
    if power <= 0:
        raise ValueError("广义均值幂必须大于0")
    total_weight = sum(float(value) for value in weights.values())
    available = {
        name: max(0.0, min(100.0, float(components[name])))
        for name in weights
        if components.get(name) is not None and isfinite(float(components[name]))
    }
    available_weight = sum(float(weights[name]) for name in available)
    coverage = 0.0 if total_weight <= 0 else available_weight / total_weight
    if coverage + 1e-12 < minimum_weight or available_weight <= 0:
        return None, coverage
    powered = sum(
        (float(weights[name]) / available_weight) * (value / 100.0) ** power
        for name, value in available.items()
    )
    return max(0.0, min(100.0, 100.0 * powered ** (1.0 / power))), coverage


def smooth_display(
    raw: float,
    previous_display: float | None,
    fast_rise_threshold: float = 8.0,
    rising_alpha: float = 0.65,
    falling_alpha: float = 0.35,
) -> float:
    value = max(0.0, min(100.0, float(raw)))
    if previous_display is None:
        return value
    previous = max(0.0, min(100.0, float(previous_display)))
    if value - previous >= fast_rise_threshold:
        return value
    alpha = rising_alpha if value >= previous else falling_alpha
    return max(0.0, min(100.0, alpha * value + (1.0 - alpha) * previous))
