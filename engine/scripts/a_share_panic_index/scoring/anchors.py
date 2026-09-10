"""固定单调锚点映射。"""

from __future__ import annotations

from math import isfinite
from typing import Sequence


def score_from_anchors(value: float, anchors: Sequence[Sequence[float]]) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("特征值必须是有限数")
    points = [(float(item[0]), float(item[1])) for item in anchors]
    if len(points) < 2:
        raise ValueError("至少需要两个锚点")
    xs = [item[0] for item in points]
    if xs != sorted(xs) or len(set(xs)) != len(xs):
        raise ValueError("锚点横坐标必须严格递增")
    if number <= points[0][0]:
        return _clip(points[0][1])
    if number >= points[-1][0]:
        return _clip(points[-1][1])
    for left, right in zip(points, points[1:]):
        if left[0] <= number <= right[0]:
            ratio = (number - left[0]) / (right[0] - left[0])
            return _clip(left[1] + ratio * (right[1] - left[1]))
    raise AssertionError("锚点插值失败")


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))
