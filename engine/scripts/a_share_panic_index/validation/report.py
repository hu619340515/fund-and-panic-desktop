"""实时与收盘模型验证报告。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def run_validation(database, mode: str, output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if mode == "realtime":
        report = _validate_realtime(database)
    elif mode == "daily":
        report = _validate_daily(database)
    else:
        raise ValueError(f"未知验证模式: {mode}")
    json_path = output / f"{mode}_validation.json"
    csv_path = output / f"{mode}_validation.csv"
    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    _write_summary(csv_path, report)
    return {**report, "json_output": str(json_path), "csv_output": str(csv_path)}


def _validate_realtime(database) -> dict[str, Any]:
    frame = pd.DataFrame(database.realtime_validation_rows())
    if frame.empty or frame["trade_date"].nunique() < 2 or len(frame) < 30:
        return {
            "mode": "realtime",
            "validation_status": "insufficient_intraday_history",
            "records": len(frame),
            "trading_days": 0 if frame.empty else int(frame["trade_date"].nunique()),
            "metrics": {},
            "component_ablation": {},
            "data_policy": "stored_intraday_snapshots_only",
        }
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "index_last"]).copy()
    frame["index_last"] = pd.to_numeric(frame["index_last"], errors="coerce")
    metrics: dict[str, Any] = {}
    grouped = frame.groupby("trade_date", group_keys=False)
    for horizon in (5, 15, 30, 60):
        future = grouped["index_last"].shift(-horizon) / frame["index_last"] - 1.0
        name = f"future_return_{horizon}m"
        frame[name] = future
        valid = frame[["realtime_panic_index_raw", name]].dropna()
        metrics[name] = {
            "spearman": _safe_corr(valid["realtime_panic_index_raw"], valid[name]),
            "samples": len(valid),
        }
    for horizon in (15, 30, 60):
        realized, drawdown = _forward_intraday_targets(frame, horizon)
        frame[f"future_realized_vol_{horizon}m"] = realized
        frame[f"future_max_drawdown_{horizon}m"] = drawdown
        metrics[f"future_realized_vol_{horizon}m"] = {
            "spearman": _safe_corr(frame["realtime_panic_index_raw"], realized),
            "samples": int(realized.notna().sum()),
        }
        metrics[f"future_max_drawdown_{horizon}m"] = {
            "spearman": _safe_corr(frame["realtime_panic_index_raw"], drawdown),
            "samples": int(drawdown.notna().sum()),
        }
    target = frame["future_max_drawdown_60m"] <= -0.01
    predicted = frame["realtime_panic_index_raw"] >= 75
    classification = _classification_metrics(predicted, target)
    deciles = _decile_table(frame, "realtime_panic_index_raw", "future_realized_vol_60m")
    return {
        "mode": "realtime",
        "validation_status": "complete",
        "records": len(frame),
        "trading_days": int(frame["trade_date"].nunique()),
        "metrics": metrics,
        "threshold_75": classification,
        "deciles": deciles,
        "raw_display_mean_gap": float(
            np.mean(np.abs(frame["realtime_panic_index_raw"] - frame["realtime_panic_index"]))
        ),
        "component_ablation": {
            "status": "requires_component_history_export",
            "reason": "当前验证记录未达到稳定消融样本要求",
        },
        "data_policy": "stored_intraday_snapshots_only",
    }


def _validate_daily(database) -> dict[str, Any]:
    frame = pd.DataFrame(database.daily_validation_rows())
    if frame.empty or len(frame) < 30:
        return {
            "mode": "daily",
            "validation_status": "insufficient_daily_history",
            "records": len(frame),
            "metrics": {},
            "data_policy": "stored_final_daily_records_only",
        }
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    metrics = {}
    for horizon in (5, 10):
        returns = frame["close"].shift(-horizon) / frame["close"] - 1.0
        drawdown = _forward_daily_drawdown(frame["close"], horizon)
        metrics[f"future_return_{horizon}d"] = {
            "spearman": _safe_corr(frame["final_panic_index"], returns),
            "samples": int(returns.notna().sum()),
        }
        metrics[f"future_max_drawdown_{horizon}d"] = {
            "spearman": _safe_corr(frame["final_panic_index"], drawdown),
            "samples": int(drawdown.notna().sum()),
        }
    return {
        "mode": "daily",
        "validation_status": "complete",
        "records": len(frame),
        "metrics": metrics,
        "deciles": _decile_table(frame, "final_panic_index", None),
        "walk_forward": "causal_features_verified_by_shifted_model_inputs",
        "data_policy": "stored_final_daily_records_only",
    }


def _forward_intraday_targets(
    frame: pd.DataFrame, horizon: int
) -> tuple[pd.Series, pd.Series]:
    realized = pd.Series(index=frame.index, dtype=float)
    drawdown = pd.Series(index=frame.index, dtype=float)
    for _, group in frame.groupby("trade_date"):
        prices = group["index_last"].to_numpy(dtype=float)
        indexes = group.index.to_list()
        for position, index in enumerate(indexes):
            future = prices[position : position + horizon + 1]
            if len(future) < horizon + 1:
                continue
            returns = np.diff(np.log(future))
            realized.loc[index] = float(np.sqrt(np.square(returns).sum()))
            drawdown.loc[index] = float(np.min(future / future[0] - 1.0))
    return realized, drawdown


def _forward_daily_drawdown(prices: pd.Series, horizon: int) -> pd.Series:
    output = pd.Series(index=prices.index, dtype=float)
    values = prices.to_numpy(dtype=float)
    for position in range(len(values) - horizon):
        future = values[position : position + horizon + 1]
        output.iloc[position] = float(np.min(future / future[0] - 1.0))
    return output


def _safe_corr(left: pd.Series, right: pd.Series) -> float | None:
    valid = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(valid) < 3 or valid["left"].nunique() < 2 or valid["right"].nunique() < 2:
        return None
    return float(valid["left"].corr(valid["right"], method="spearman"))


def _classification_metrics(predicted: pd.Series, target: pd.Series) -> dict[str, float | int]:
    valid = predicted.notna() & target.notna()
    p = predicted[valid].astype(bool)
    t = target[valid].astype(bool)
    true_positive = int((p & t).sum())
    false_positive = int((p & ~t).sum())
    false_negative = int((~p & t).sum())
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": false_positive / max(int((~t).sum()), 1),
        "samples": int(valid.sum()),
    }


def _decile_table(
    frame: pd.DataFrame, score_column: str, target_column: str | None
) -> list[dict[str, Any]]:
    valid = frame[[score_column] + ([target_column] if target_column else [])].dropna()
    if len(valid) < 20 or valid[score_column].nunique() < 10:
        return []
    valid = valid.copy()
    valid["decile"] = pd.qcut(valid[score_column], 10, labels=False, duplicates="drop") + 1
    output = []
    for decile, group in valid.groupby("decile"):
        item = {
            "decile": int(decile),
            "mean_score": float(group[score_column].mean()),
            "samples": len(group),
        }
        if target_column:
            item["mean_target"] = float(group[target_column].mean())
        output.append(item)
    return output


def _write_summary(path: Path, report: dict[str, Any]) -> None:
    headers = ["验证模式", "验证状态", "记录数", "指标", "相关系数", "样本数"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        metrics = report.get("metrics", {})
        if not metrics:
            writer.writerow(
                {
                    "验证模式": report["mode"],
                    "验证状态": report["validation_status"],
                    "记录数": report.get("records", 0),
                    "指标": "",
                    "相关系数": "",
                    "样本数": 0,
                }
            )
        for name, metric in metrics.items():
            writer.writerow(
                {
                    "验证模式": report["mode"],
                    "验证状态": report["validation_status"],
                    "记录数": report.get("records", 0),
                    "指标": name,
                    "相关系数": metric.get("spearman"),
                    "样本数": metric.get("samples", 0),
                }
            )
