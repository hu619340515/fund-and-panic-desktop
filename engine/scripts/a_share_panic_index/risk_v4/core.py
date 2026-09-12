"""仅以已发生的日线信息构造稳定口径的当前市场状态。"""

from __future__ import annotations

from datetime import date
from math import isfinite, log, sqrt
from statistics import pstdev
from typing import Any
from .spec import CONFIG

VERSION = CONFIG["version"]
MIN_REFERENCE = CONFIG["core"]["minimum_reference"]
MAX_REFERENCE = CONFIG["core"]["maximum_reference"]
WEIGHTS = dict(CONFIG["core"]["weights"])
OVERHEAT = ("upstretch", "ma60_stretch", "amount20_stretch")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _prepare_rows(rows: list[dict[str, Any]], as_of: str | None = None) -> list[dict[str, Any]]:
    """保留无效交易日为缺口；绝不向前填充价格。"""
    result = []
    seen = set()
    cutoff = date.fromisoformat(as_of) if as_of else None
    for item in rows:
        day = date.fromisoformat(str(item["trade_date"])[:10])
        if cutoff and day > cutoff:
            continue
        if day in seen:
            raise ValueError(f"重复交易日: {day.isoformat()}")
        seen.add(day)
        row = dict(item)
        row["trade_date"] = day.isoformat()
        for key in ("open", "high", "low", "close", "amount"):
            row[key] = _number(item.get(key))
        # 日终状态与未来收盘收益只依赖已观测收盘；交易回测另需真实开盘价。
        row["_valid"] = row["close"] is not None and row["close"] > 0
        row["_execution_valid"] = (row["_valid"] and row["open"] is not None
                                   and row["open"] > 0)
        result.append(row)
    return sorted(result, key=lambda row: row["trade_date"])


def _feature_at(rows: list[dict[str, Any]], index: int) -> dict[str, float | None] | None:
    if (index < 60 or not all(row["_valid"] for row in rows[index - 60:index + 1])
            or any(row.get("gap_before") for row in rows[index - 59:index + 1])):
        return None
    closes = [float(row["close"]) for row in rows[index - 60:index + 1]]
    returns = [log(closes[j] / closes[j - 1]) for j in range(1, 61)]
    sigma_before = pstdev(returns[-21:-1])
    shock = max(0.0, -log(closes[-1] / closes[-6])) / max(sigma_before * sqrt(5), 1e-9)
    highest_close = max(closes[-60:])
    drawdown = max(0.0, 1.0 - closes[-1] / highest_close)
    downside = sqrt(sum(min(ret, 0.0) ** 2 for ret in returns[-20:]) / 20.0) * sqrt(252)
    upstretch = max(0.0, log(closes[-1] / closes[-21])) / max(sigma_before * sqrt(20), 1e-9)
    ma60_stretch = max(0.0, closes[-1] / (sum(closes[-60:]) / 60.0) - 1.0)
    amounts = [row["amount"] for row in rows[index - 20:index + 1]]
    amount20_stretch = None
    if all(value is not None and value > 0 for value in amounts):
        median_amount = sorted(float(value) for value in amounts[:-1])[9:11]
        baseline = sum(median_amount) / 2.0
        amount20_stretch = max(0.0, float(amounts[-1]) / baseline - 1.0)
    return {"shock": shock, "drawdown": drawdown, "downside": downside,
            "upstretch": upstretch, "ma60_stretch": ma60_stretch,
            "amount20_stretch": amount20_stretch,
            "return_20": log(closes[-1] / closes[-21]),
            "volatility_20": pstdev(returns[-20:]) * sqrt(252)}


def _percentile(value: float, history: list[float]) -> float:
    if value <= 0:
        return 0.0
    return 100.0 * (sum(item < value for item in history) +
                    0.5 * sum(item == value for item in history)) / len(history)


def _five_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 5)
    except ValueError:
        return day.replace(year=day.year - 5, day=28)


def _states(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_features: list[dict[str, float | None] | None] = []
    output = []
    for index, row in enumerate(rows):
        raw = _feature_at(rows, index)
        start = max(0, index - MAX_REFERENCE)
        earliest = _five_years_before(date.fromisoformat(row["trade_date"]))
        history = [item for past, item in zip(rows[start:index], raw_features[start:index])
                   if past["trade_date"] >= earliest.isoformat() and item is not None]
        missing = []
        if raw is None:
            missing.append("近60个交易日真实、连续的收盘价")
        if len(history) < MIN_REFERENCE:
            missing.append(f"前日可用特征历史至少{MIN_REFERENCE}日（当前{len(history)}日）")
        core_missing = bool(missing)
        components: dict[str, dict[str, float | None]] = {}
        score = overheat = None
        if raw is not None:
            for key in WEIGHTS:
                components[key] = {"raw": raw[key], "percentile": None}
        if raw is not None and not core_missing:
            for key in WEIGHTS:
                components[key]["percentile"] = _percentile(float(raw[key]), [float(item[key]) for item in history])
            score = sum(WEIGHTS[key] * float(components[key]["percentile"]) for key in WEIGHTS)
            heat_components = {}
            for key in OVERHEAT:
                past = [float(item[key]) for item in history if item[key] is not None]
                if raw[key] is None or len(past) < MIN_REFERENCE:
                    missing.append(f"过热项{key}需要当日及至少{MIN_REFERENCE}个已知历史值")
                else:
                    heat_components[key] = _percentile(float(raw[key]), past)
            if len(heat_components) == len(OVERHEAT):
                overheat = sum(heat_components.values()) / len(OVERHEAT)
                components["overheat"] = {key: {"raw": raw[key], "percentile": heat_components[key]}
                                          for key in OVERHEAT}
        output.append({"state": "ready" if score is not None else "insufficient_data",
                       "as_of": row["trade_date"], "score": score, "components": components,
                       "overheat": overheat,
                       "explanation": (["三项压力分别按截至前一交易日的历史排序；0表示无该项压力。"]
                                       if score is not None else ["缺少完整的无前视参考样本。"]),
                       "missing": missing, "model_version": VERSION})
        raw_features.append(raw)
    return output


def compute_history(rows: list[dict[str, Any]], limit: int = 252) -> list[dict[str, Any]]:
    if limit < 0:
        raise ValueError("limit 不能为负")
    prepared = _prepare_rows(rows)
    return _states(prepared)[-limit:] if limit else []


def compute_state(rows: list[dict[str, Any]], as_of: str | None = None) -> dict[str, Any]:
    prepared = _prepare_rows(rows, as_of)
    if not prepared:
        return {"state": "insufficient_data", "as_of": as_of, "score": None,
                "components": {}, "overheat": None, "explanation": ["没有有效日期的日线。"],
                "missing": ["日线"], "model_version": VERSION}
    return _states(prepared)[-1]
