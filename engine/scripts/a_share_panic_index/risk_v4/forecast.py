"""逐时点训练的分段危险率与线性分位数回归；artifact 可直接 JSON 序列化。"""

from __future__ import annotations

from math import erfc, log, sqrt
from typing import Any, Callable

import numpy as np

from .core import VERSION, _feature_at, _prepare_rows
from .spec import CONFIG,CONFIG_SHA256

HORIZONS = tuple(CONFIG["forecast"]["horizons"])
THRESHOLDS = tuple(CONFIG["forecast"]["drawdown_thresholds"])
QUANTILES = tuple(CONFIG["forecast"]["return_quantiles"])
FEATURE_NAMES = ("shock", "drawdown", "downside", "return_20", "volatility_20")
MIN_TRAIN, MIN_CALIBRATION, EMBARGO, MIN_OOS = (CONFIG["forecast"][key] for key in ("minimum_train","minimum_calibration","embargo","minimum_out_of_sample"))


def _dependencies():
    try:
        from sklearn.linear_model import LogisticRegression, QuantileRegressor
        from sklearn.preprocessing import StandardScaler
    except ImportError as error:
        raise RuntimeError("训练预测模型需要 scikit-learn 和 scipy") from error
    return LogisticRegression, QuantileRegressor, StandardScaler


def _samples(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    samples = []
    latest = None
    for index, row in enumerate(rows):
        values = _feature_at(rows, index)
        if values is None:
            continue
        item = {"date": row["trade_date"], "index": index,
                "x": [values[name] for name in FEATURE_NAMES]}
        latest = item
        # 标签是未来真实收盘价；OHLC 中缺少旧开盘/盘中高低不影响日终研究样本。
        if index + 50 >= len(rows) or not all(other["_valid"] and not other.get("gap_before")
                                                for other in rows[index + 1:index + 51]):
            continue
        base = float(row["close"])
        future = [float(other["close"]) / base - 1.0 for other in rows[index + 1:index + 51]]
        item["minimum"] = {str(h): min(future[:h]) for h in HORIZONS}
        item["terminal"] = {str(h): future[h - 1] for h in HORIZONS}
        item["event"] = {
            f"{h}d_{threshold}pct": int(item["minimum"][str(h)] <= -threshold / 100.0)
            for h in HORIZONS for threshold in THRESHOLDS
        }
        samples.append(item)
    if rows and latest and latest["date"] != rows[-1]["trade_date"]:
        latest = None
    return samples, latest


def _scale(train: list[dict[str, Any]]):
    _, _, StandardScaler = _dependencies()
    scaler = StandardScaler().fit(np.asarray([item["x"] for item in train], dtype=float))
    return {"mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist()}


def _transform(items: list[dict[str, Any]], scaler: dict[str, Any]) -> np.ndarray:
    values = np.asarray([item["x"] for item in items], dtype=float)
    if values.size == 0:
        return np.empty((0, len(FEATURE_NAMES)))
    return (values - np.asarray(scaler["mean"])) / np.asarray(scaler["scale"])


def _sigmoid(value: np.ndarray) -> np.ndarray:
    return np.where(value >= 0, 1.0 / (1.0 + np.exp(-np.clip(value, -700, 700))),
                    np.exp(np.clip(value, -700, 700)) / (1.0 + np.exp(np.clip(value, -700, 700))))


def _fit_hazards(train: list[dict[str, Any]], scaler: dict[str, Any]) -> dict[str, Any]:
    LogisticRegression, _, _ = _dependencies()
    x = _transform(train, scaler)
    models = {}
    for threshold in THRESHOLDS:
        previous = np.zeros(len(train), dtype=bool)
        for h in HORIZONS:
            cumulative = np.asarray([item["event"][f"{h}d_{threshold}pct"] for item in train], dtype=bool)
            eligible = ~previous
            y = cumulative[eligible].astype(int)
            key = f"{h}d_{threshold}pct"
            if len(np.unique(y)) < 2:
                models[key] = {"type": "constant", "probability": float((y.sum() + 1) / (len(y) + 2))}
            else:
                model = LogisticRegression(C=CONFIG["forecast"]["logistic_c"], max_iter=300, solver="lbfgs").fit(x[eligible], y)
                models[key] = {"type": "logistic", "coef": model.coef_[0].tolist(),
                               "intercept": float(model.intercept_[0])}
            previous = cumulative
    return models


def _raw_probabilities(x: np.ndarray, models: dict[str, Any]) -> dict[str, np.ndarray]:
    results: dict[str, np.ndarray] = {}
    for threshold in THRESHOLDS:
        survival = np.ones(len(x))
        for h in HORIZONS:
            key = f"{h}d_{threshold}pct"
            model = models[key]
            if model["type"] == "constant":
                hazard = np.full(len(x), model["probability"])
            else:
                hazard = _sigmoid(x @ np.asarray(model["coef"]) + model["intercept"])
            survival *= 1.0 - hazard
            results[key] = 1.0 - survival.copy()
    return results


def _fit_calibrators(calibration: list[dict[str, Any]], scaler: dict[str, Any],
                     models: dict[str, Any]) -> dict[str, Any]:
    LogisticRegression, _, _ = _dependencies()
    raw = _raw_probabilities(_transform(calibration, scaler), models)
    calibrators = {}
    for key, values in raw.items():
        y = np.asarray([item["event"][key] for item in calibration], dtype=int)
        if len(np.unique(y)) < 2:
            calibrators[key] = {"type": "identity"}
            continue
        logits = np.log(np.clip(values, 1e-6, 1 - 1e-6) / np.clip(1 - values, 1e-6, 1))
        fit = LogisticRegression(C=1.0, max_iter=300).fit(logits.reshape(-1, 1), y)
        calibrators[key] = {"type": "logistic", "coef": float(fit.coef_[0][0]),
                            "intercept": float(fit.intercept_[0])}
    return calibrators


def _probabilities(items: list[dict[str, Any]], artifact: dict[str, Any]) -> dict[str, np.ndarray]:
    x = _transform(items, artifact["scaler"])
    raw = _raw_probabilities(x, artifact["hazards"])
    calibrated = {}
    for key, values in raw.items():
        model = artifact["calibrators"][key]
        if model["type"] == "logistic":
            logits = np.log(np.clip(values, 1e-6, 1 - 1e-6) / np.clip(1 - values, 1e-6, 1))
            values = _sigmoid(model["coef"] * logits + model["intercept"])
        calibrated[key] = values
    # 单调投影：期限越长事件越宽，损失阈值越低事件越宽。
    for threshold in THRESHOLDS:
        previous = np.zeros(len(items))
        for h in HORIZONS:
            key = f"{h}d_{threshold}pct"
            calibrated[key] = np.maximum(previous, calibrated[key])
            previous = calibrated[key]
    for h in HORIZONS:
        previous = np.zeros(len(items))
        for threshold in reversed(THRESHOLDS):
            key = f"{h}d_{threshold}pct"
            calibrated[key] = np.maximum(previous, calibrated[key])
            previous = calibrated[key]
    return calibrated


def _fit_quantiles(train: list[dict[str, Any]], scaler: dict[str, Any]) -> dict[str, Any]:
    _, QuantileRegressor, _ = _dependencies()
    x = _transform(train, scaler)
    models = {}
    for h in HORIZONS:
        y = np.asarray([item["terminal"][str(h)] for item in train])
        for quantile in QUANTILES:
            fit = QuantileRegressor(quantile=quantile, alpha=0.05, solver="highs").fit(x, y)
            models[f"{h}d_q{int(quantile * 100)}"] = {
                "coef": fit.coef_.tolist(), "intercept": float(fit.intercept_)
            }
    return models


def _quantiles(items: list[dict[str, Any]], artifact: dict[str, Any]) -> dict[str, np.ndarray]:
    x = _transform(items, artifact["scaler"])
    result = {}
    for h in HORIZONS:
        previous = np.full(len(items), -np.inf)
        for quantile in QUANTILES:
            key = f"{h}d_q{int(quantile * 100)}"
            model = artifact["quantiles"][key]
            values = x @ np.asarray(model["coef"]) + model["intercept"]
            result[key] = np.maximum(previous, values)
            previous = result[key]
    return result


def _ece(y: np.ndarray, probabilities: np.ndarray) -> float:
    edges = np.linspace(0, 1, 11)
    total = 0.0
    for index in range(10):
        mask = (probabilities >= edges[index]) & (probabilities <= edges[index + 1] if index == 9 else probabilities < edges[index + 1])
        if mask.any():
            total += float(mask.mean() * abs(y[mask].mean() - probabilities[mask].mean()))
    return total


def _simple_volatility_probabilities(items: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    """无漂移连续 Brownian 对数价格的触及近似；只报告基线，不参与发布判定。"""
    results = {f"{h}d_{threshold}pct": [] for h in HORIZONS for threshold in THRESHOLDS}
    vol_index = FEATURE_NAMES.index("volatility_20")
    for item in items:
        # 特征是截至信号日的最近20日年化对数收益波动，不读取任何未来标签。
        daily_sigma = max(0.0, float(item["x"][vol_index])) / sqrt(252.0)
        for h in HORIZONS:
            for threshold in THRESHOLDS:
                barrier = -log(1.0 - threshold / 100.0)
                probability = erfc(barrier / (daily_sigma * sqrt(2.0 * h))) if daily_sigma > 0 else 0.0
                results[f"{h}d_{threshold}pct"].append(min(1.0, probability))
    return {key: np.asarray(values, dtype=float) for key, values in results.items()}


def _block_ci(loss_difference: np.ndarray, block: int = 50) -> list[float]:
    blocks = [loss_difference[start:start + block] for start in range(0, len(loss_difference), block)]
    rng = np.random.default_rng(20260912)
    means = []
    for _ in range(300):
        choices = rng.integers(0, len(blocks), size=len(blocks))
        sample = np.concatenate([blocks[index] for index in choices])
        means.append(float(sample.mean()))
    return np.quantile(means, [0.025, 0.975]).tolist()


def _empty(symbol: str, reason: str, pit: bool) -> dict[str, Any]:
    validation = {"status": "insufficient_data", "publishable": False,
                  "fully_publishable": False, "published_events": [], "published_intervals": [],
                  "pit_verified": pit, "events": {}, "reasons": [reason], "oos_samples": 0}
    return {"model_version": VERSION, "symbol": symbol, "artifact": {},
            "validation": validation, "latest": {"state": "unavailable", "as_of": None,
            "raw_research": {}, "published": {}, "quantiles": {}, "reasons": [reason]}}


def _fit(train: list[dict[str, Any]], calibration: list[dict[str, Any]], *,
         through: str, symbol: str, quantiles: bool) -> dict[str, Any]:
    scaler = _scale(train)
    hazards = _fit_hazards(train, scaler)
    calibrators = _fit_calibrators(calibration, scaler, hazards)
    return {"version": VERSION, "configuration_sha256":CONFIG_SHA256,"symbol": symbol, "trained_through": through,
            "calibrated_through": calibration[-1]["date"], "feature_names": list(FEATURE_NAMES),
            "scaler": scaler, "hazards": hazards, "calibrators": calibrators,
            "quantiles": _fit_quantiles(train, scaler) if quantiles else {},
            "validation": {"publishable": False, "pit_verified": False}}


def train_and_validate(rows: list[dict[str, Any]], *, symbol: str,
                       progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    prepared = _prepare_rows(rows)
    pit = bool(prepared) and all(row.get("pit_verified") is True and row.get("published_at")
                                 and row.get("source_id") and row.get("fetched_at")
                                 and str(row["published_at"])[:10] <= row["trade_date"]
                                 for row in prepared)
    samples, latest = _samples(prepared)
    first_test = MIN_TRAIN + EMBARGO + MIN_CALIBRATION + EMBARGO
    if len(samples) - first_test < MIN_OOS:
        result = _empty(symbol, f"成熟标签样本不足：需至少{first_test + MIN_OOS}，现有{len(samples)}", pit)
        result["latest"]["as_of"] = latest["date"] if latest else None
        return result
    _dependencies()
    predicted: dict[str, list[float]] = {f"{h}d_{threshold}pct": [] for h in HORIZONS for threshold in THRESHOLDS}
    baselines: dict[str, list[float]] = {key: [] for key in predicted}
    volatility_baselines: dict[str, list[float]] = {key: [] for key in predicted}
    quantile_predicted: dict[str, list[float]] = {f"{h}d_q{int(q * 100)}": [] for h in HORIZONS for q in QUANTILES}
    test_items = []
    for start in range(first_test, len(samples), 252):
        train_end = start - EMBARGO - MIN_CALIBRATION - EMBARGO
        cal_start, cal_end = train_end + EMBARGO, start - EMBARGO
        train, calibration = samples[:train_end], samples[cal_start:cal_end]
        test = samples[start:min(start + 252, len(samples))]
        artifact = _fit(train, calibration, through=train[-1]["date"], symbol=symbol, quantiles=True)
        probabilities = _probabilities(test, artifact)
        quantiles = _quantiles(test, artifact)
        simple_volatility = _simple_volatility_probabilities(test)
        for key in predicted:
            predicted[key].extend(probabilities[key].tolist())
            baselines[key].extend([sum(item["event"][key] for item in train) / len(train)] * len(test))
            volatility_baselines[key].extend(simple_volatility[key].tolist())
        for key in quantile_predicted:
            quantile_predicted[key].extend(quantiles[key].tolist())
        test_items.extend(test)
        if progress:
            progress({"stage": "walk_forward", "tested": len(test_items), "total": len(samples) - first_test})
    metrics = {}
    reasons = []
    for key, probabilities in predicted.items():
        y = np.asarray([item["event"][key] for item in test_items])
        p, baseline = np.asarray(probabilities), np.asarray(baselines[key])
        brier, base_brier = float(np.mean((p - y) ** 2)), float(np.mean((baseline - y) ** 2))
        simple_volatility_brier = float(np.mean((np.asarray(volatility_baselines[key]) - y) ** 2))
        gain = (baseline - y) ** 2 - (p - y) ** 2
        interval = _block_ci(gain)
        positives, negatives = int(y.sum()), int(len(y) - y.sum())
        ece = _ece(y, p)
        passed = positives >= 100 and negatives >= 100 and brier < base_brier and ece <= 0.05 and interval[0] > 0
        metrics[key] = {"positives": positives, "negatives": negatives, "brier": brier,
                        "baseline_brier": base_brier, "simple_volatility_brier": simple_volatility_brier,
                        "ece": ece,
                        "brier_gain_95ci_block50": interval, "passed": bool(passed)}
        if not passed:
            reasons.append(f"{key}未通过样本数、Brier、ECE或时间块置信区间门槛")
    interval_metrics = {}
    for h in HORIZONS:
        actual = np.asarray([item["terminal"][str(h)] for item in test_items])
        lower = np.asarray(quantile_predicted[f"{h}d_q10"])
        upper = np.asarray(quantile_predicted[f"{h}d_q90"])
        coverage = float(np.mean((actual >= lower) & (actual <= upper)))
        interval_metrics[f"{h}d"] = {"coverage_10_90": coverage, "passed": 0.75 <= coverage <= 0.85}
        if not interval_metrics[f"{h}d"]["passed"]:
            reasons.append(f"{h}d分位数区间覆盖率未通过")
    if not pit:
        reasons.append("输入历史的发布时间、来源或pit_verified无法逐行核实")
    passed_events = [key for key, item in metrics.items() if item["passed"]]
    passed_intervals = [key for key, item in interval_metrics.items() if item["passed"]]
    publishable = pit and bool(passed_events or passed_intervals)
    fully_publishable = pit and not reasons
    validation = {"status": "passed" if fully_publishable else "partial" if publishable else "research_only",
                  "publishable": publishable, "fully_publishable": fully_publishable,
                  "pit_verified": pit, "events": metrics, "intervals": interval_metrics,
                  "published_events": passed_events if pit else [],
                  "published_intervals": passed_intervals if pit else [],
                  "simple_volatility_assumption": "前20日实现波动、对数价格无漂移且连续Brownian触及近似；仅比较OOS Brier，不作为正式发布门槛",
                  "reasons": reasons, "oos_samples": len(test_items), "folds": (len(test_items) + 251) // 252,
                  "definition": "风险事件=未来H交易日最低收盘价/信号日收盘价-1；分位数=第H日收盘价/信号日收盘价-1；均非下一开盘可成交收益"}
    # 当前发布参数只在标签已完整成熟的历史上拟合；训练与校准仍隔离50日。
    cal_start = len(samples) - MIN_CALIBRATION
    final_train = samples[:cal_start - EMBARGO]
    final_calibration = samples[cal_start:]
    artifact = _fit(final_train, final_calibration, through=final_train[-1]["date"],
                    symbol=symbol, quantiles=True)
    artifact["validation"] = {"publishable": publishable, "fully_publishable": fully_publishable,
                              "pit_verified": pit, "published_events": passed_events if pit else [],
                              "published_intervals": passed_intervals if pit else [],
                              "oos_samples": len(test_items), "reasons": reasons}
    latest_result = predict(rows, artifact)
    oos_predictions = []
    for index, item in enumerate(test_items):
        oos_predictions.append({"state": "research_only", "as_of": item["date"],
                                "raw_research": {key: predicted[key][index] for key in predicted},
                                "simple_volatility": {key: volatility_baselines[key][index] for key in predicted},
                                "quantiles": {key: quantile_predicted[key][index]
                                              for key in quantile_predicted},
                                "published": {}, "walk_forward_oos": True})
    return {"model_version": VERSION, "symbol": symbol, "artifact": artifact,
            "validation": validation, "latest": latest_result,
            "oos_predictions": oos_predictions}


def predict(rows: list[dict[str, Any]], artifact: dict[str, Any]) -> dict[str, Any]:
    prepared = _prepare_rows(rows)
    _, latest = _samples(prepared)
    result = {"state": "unavailable", "as_of": latest["date"] if latest else None,
              "raw_research": {}, "published": {}, "quantiles": {}, "reasons": []}
    if not artifact or not latest:
        result["reasons"].append("缺少模型参数或完整近期日线")
        return result
    if artifact.get("version") != VERSION or artifact.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("模型版本或特征定义不匹配")
    if artifact.get("configuration_sha256")!=CONFIG_SHA256:
        raise ValueError("模型配置已经变更，请重新训练和验证")
    if latest["date"] <= artifact["calibrated_through"]:
        result["reasons"].append("预测日期不晚于参数校准截止日")
        return result
    probability = {key: float(value[0]) for key, value in _probabilities([latest], artifact).items()}
    quantile = {key: float(value[0]) for key, value in _quantiles([latest], artifact).items()}
    result.update(state="research_only", raw_research=probability, quantiles=quantile)
    verified = all(row.get("pit_verified") is True and row.get("published_at")
                   and row.get("source_id") and row.get("fetched_at")
                   and str(row["published_at"])[:10] <= row["trade_date"] for row in prepared)
    gate = artifact.get("validation", {})
    if gate.get("publishable") is True and gate.get("pit_verified") is True and verified:
        selected_probabilities = {key: probability[key] for key in gate.get("published_events", [])
                                  if key in probability}
        selected_quantiles = {key: value for key, value in quantile.items()
                              if key.split("_")[0] in gate.get("published_intervals", [])}
        if selected_probabilities or selected_quantiles:
            result.update(state="published", published={"probabilities": selected_probabilities,
                                                         "quantiles": selected_quantiles})
        else:
            result["reasons"].append("发布白名单中没有与当前模型匹配的事件或期限")
    else:
        result["reasons"].append("PIT或时间外验证未通过；数值仅供研究")
    return result
