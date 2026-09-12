"""把已验证状态转换为保守、可审计的观察或操作信号。"""

from __future__ import annotations

from math import isfinite
from typing import Any


def signal_conditions(states: list[dict[str, Any]], forecasts: list[dict[str, Any]],
                      closes: list[float], *, level: float, confirmation_days: int,
                      mode: str, cost_bps_per_side: float, published_only: bool) -> bool:
    """在线和历史回测共用的条件：入场须收盘价逐日企稳且收益覆盖成本。"""
    count = int(confirmation_days)
    if (count not in {3, 5} or mode not in {"entry", "exit"}
            or len(states) < count or len(forecasts) < count or len(closes) < count
            or cost_bps_per_side < 0):
        return False
    recent_closes = [float(value) for value in closes[-count:]]
    if not all(isfinite(value) and value > 0 for value in recent_closes):
        return False
    if mode == "entry" and any(next_close < prior_close
                               for prior_close, next_close in zip(recent_closes, recent_closes[1:])):
        return False
    round_trip_cost = 2.0 * cost_bps_per_side / 10000.0
    for past, prediction in zip(states[-count:], forecasts[-count:]):
        if past.get("as_of") and prediction.get("as_of") and past["as_of"] != prediction["as_of"]:
            return False
        published = prediction.get("published") or {}
        probabilities = (published.get("probabilities") or {}) if published_only else (
            prediction.get("raw_research") or published.get("probabilities") or {})
        quantiles = (published.get("quantiles") or {}) if published_only else (
            prediction.get("quantiles") or published.get("quantiles") or {})
        risk = probabilities.get("20d_5pct")
        if (past.get("state") != "ready" or risk is None
                or (published_only and prediction.get("state") != "published")):
            return False
        if mode == "entry":
            q10, q50 = quantiles.get("20d_q10"), quantiles.get("20d_q50")
            if (past.get("score") is None or past["score"] < level or risk > 0.20
                    or q10 is None or q50 is None or q50 <= round_trip_cost
                    or q10 < -0.05 + round_trip_cost):
                return False
        elif past.get("overheat") is None or past["overheat"] < level or risk < 0.20:
            return False
    return True


def evaluate_signal(state: dict[str, Any], forecast: dict[str, Any],
                    context: dict[str, Any]) -> dict[str, Any]:
    style = str(context.get("style") or "balanced")
    if style not in {"conservative", "balanced", "aggressive"}:
        raise ValueError("style 必须为 conservative、balanced 或 aggressive")
    result = {"action": "observe", "eligible": False, "reason": "", "style": style,
              "details": {"state_score": state.get("score"), "overheat": state.get("overheat"),
                          "model_version": state.get("model_version")}}
    if state.get("state") != "ready" or forecast.get("state") != "published":
        result["reason"] = "状态历史或未来风险概率尚未通过验证，仅供观察。"
        return result
    if int(context.get("live_observation_days") or 0) < 20:
        result["reason"] = "实盘观察不足20个交易日，暂不启用操作建议。"
        return result
    validation = context.get("strategy_validation") or {}
    if validation.get("enabled") is not True or validation.get("status") != "passed":
        result["reason"] = "独立、含交易成本的策略时间外回测未达启用门槛，仅供观察。"
        return result
    probabilities = forecast.get("published", {}).get("probabilities", {})
    risk = probabilities.get("20d_5pct")
    if risk is None:
        result["reason"] = "缺少经验证的20日5%下跌概率。"
        return result
    # 风格只选择已经验证过的状态门槛；10%为回测过的最大风险预算。
    settings = {"conservative": (95.0, 0.10), "balanced": (85.0, 0.10),
                "aggressive": (75.0, 0.10)}
    level, max_budget = settings[style]
    result["details"].update({"risk_20d_5pct": risk, "max_new_position_fraction": max_budget})
    selected_exit = validation.get("selected_exit") or {}
    selected_entry = validation.get("selected") or {}
    states = list(context.get("recent_states") or [])
    forecasts = list(context.get("recent_forecasts") or [])
    closes = list(context.get("recent_closes") or [])
    if not states or states[-1].get("as_of") != state.get("as_of"):
        states.append(state)
        forecasts.append(forecast)
        if context.get("close") is not None:
            closes.append(context["close"])
    cost = float(validation.get("cost_bps_per_side", 10.0))
    if (context.get("has_position") and validation.get("exit_enabled") is True
            and selected_exit.get("level") == level
            and signal_conditions(states, forecasts, closes, level=level,
                                  confirmation_days=selected_exit["confirmation_days"],
                                  mode="exit", cost_bps_per_side=cost, published_only=True)):
        result.update(action="review_exit", eligible=True,
                      reason="过热达到风格门槛；检查持仓、成本和再平衡约束后决定是否减仓。")
    elif (validation.get("entry_enabled") is True and selected_entry.get("level") == level
          and signal_conditions(states, forecasts, closes, level=level,
                                confirmation_days=selected_entry["confirmation_days"],
                                mode="entry", cost_bps_per_side=cost, published_only=True)):
        result.update(action="review_entry", eligible=True,
                      reason="恐慌与未来风险同时达到已验证策略门槛；风险预算是上限而非精确仓位。")
    else:
        result["reason"] = "未同时满足已验证网格的状态、价格企稳、20日风险与扣费收益门槛。"
    return result
