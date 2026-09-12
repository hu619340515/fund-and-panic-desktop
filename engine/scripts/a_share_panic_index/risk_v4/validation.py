"""策略独立时间外检验；交易发生在信号之后并计入显式成本。"""

from __future__ import annotations

from math import log
from statistics import median, pstdev
from typing import Any

import numpy as np

from .core import _prepare_rows
from .strategy import signal_conditions

GRID = tuple((level, stable_days, holding) for level in (75, 85, 95)
             for stable_days in (3, 5) for holding in (5, 20))
RISK_BUDGET = 0.10


def _drawdown(returns: list[float]) -> float:
    wealth = peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1.0)
    return worst


def _confirmed(index: int, rows: list[dict[str, Any]], states: dict[str, dict[str, Any]],
               forecasts: dict[str, dict[str, Any]], level: int, stable_days: int,
               mode: str, cost_bps: float) -> bool:
    if index < stable_days - 1:
        return False
    recent = rows[index - stable_days + 1:index + 1]
    return signal_conditions([states.get(row["trade_date"], {}) for row in recent],
                             [forecasts.get(row["trade_date"], {}) for row in recent],
                             [row["close"] for row in recent], level=level,
                             confirmation_days=stable_days, mode=mode,
                             cost_bps_per_side=cost_bps, published_only=False)


def _simple_signal(index: int, rows: list[dict[str, Any]], mode: str) -> bool:
    if (index < 81 or not all(row["_valid"] for row in rows[index - 81:index + 1])
            or any(row.get("gap_before") for row in rows[index - 80:index + 1])):
        return False
    prices = [float(row["close"]) for row in rows[index - 81:index + 1]]
    if mode == "trend":
        return prices[-1] > sum(prices[-20:]) / 20.0
    returns = [log(prices[j] / prices[j - 1]) for j in range(1, len(prices))]
    current = pstdev(returns[-20:])
    past = [pstdev(returns[j - 20:j]) for j in range(21, len(returns))]
    return current > median(past)


def _simulate(rows: list[dict[str, Any]], states: dict[str, dict[str, Any]],
              forecasts: dict[str, dict[str, Any]], start: int, end: int,
              level: int, stable_days: int, holding: int, cost_bps: float,
              mode: str) -> dict[str, Any]:
    """同日逐日净值：信号后下一开盘买卖，每日收盘盯市，结束时收盘清算。"""
    cost = cost_bps / 10000.0
    cash, shares, nav = 1.0, 0.0, 1.0
    nav_series, events, turnover = [], 0, 0.0
    switch_end = -1
    for index in range(start + 1, end + 1):
        if not rows[index]["_execution_valid"]:
            raise ValueError("策略执行区间缺少真实开盘或收盘报价")
        open_price = float(rows[index]["open"])
        close_price = float(rows[index]["close"])
        pre_open_nav = cash + shares * open_price
        prior = index - 1
        if mode == "baseline" and index == start + 1:
            stake = nav * RISK_BUDGET
            turnover += stake / pre_open_nav
            cash -= stake * (1 + cost)
            shares = stake / open_price
        elif mode in {"entry", "trend", "volatility"}:
            triggered = (_confirmed(prior, rows, states, forecasts, level, stable_days, "entry", cost_bps)
                         if mode == "entry" else _simple_signal(prior, rows, mode))
            if (not shares and index > switch_end and index + holding - 1 <= end
                    and triggered):
                stake = nav * RISK_BUDGET
                turnover += stake / pre_open_nav
                cash -= stake * (1 + cost)
                shares = stake / open_price
                switch_end = index + holding - 1
                events += 1
        elif mode == "exit":
            if index == start + 1:
                stake = nav * RISK_BUDGET
                turnover += stake / pre_open_nav
                cash -= stake * (1 + cost)
                shares = stake / open_price
            elif (shares and index > switch_end and index + holding - 1 <= end
                  and _confirmed(prior, rows, states, forecasts, level, stable_days, "exit", cost_bps)):
                turnover += shares * open_price / pre_open_nav
                cash += shares * open_price * (1 - cost)
                shares = 0.0
                switch_end = index + holding - 1
                events += 1
        if mode in {"entry", "trend", "volatility"} and shares and index == switch_end:
            turnover += shares * close_price / (cash + shares * close_price)
            cash += shares * close_price * (1 - cost)
            shares = 0.0
        elif mode == "exit" and not shares and index == switch_end:
            stake = (cash / (1 + cost)) * RISK_BUDGET
            turnover += stake / cash
            cash -= stake * (1 + cost)
            shares = stake / close_price
        nav = cash + shares * close_price
        nav_series.append(nav)
    if shares:
        turnover += shares * float(rows[end]["close"]) / (cash + shares * float(rows[end]["close"]))
        cash += shares * float(rows[end]["close"]) * (1 - cost)
        nav_series[-1] = cash
    daily_returns = [nav_series[0] - 1.0] + [nav_series[j] / nav_series[j - 1] - 1.0
                                               for j in range(1, len(nav_series))]
    return {"trades": events, "turnover": turnover, "net_return": nav_series[-1] - 1.0,
            "max_drawdown": _drawdown(daily_returns), "daily_returns": daily_returns,
            "daily_nav": nav_series}


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in ("trades", "net_return", "max_drawdown", "turnover")}


def _development_rank(item: dict[str, Any]) -> tuple[float, float]:
    """仅用开发段：扣费超额收益＋最大回撤改善；同分取较低真实换手。"""
    return (item["development_selection_score"] if item["development"]["trades"] >= 12 else -float("inf"),
            -item["development"]["turnover"])


def _block_ci(daily_excess: list[float], block: int = 20) -> list[float]:
    chunks = [daily_excess[index:index + block] for index in range(0, len(daily_excess), block)]
    rng = np.random.default_rng(20260912)
    means = [float(np.mean(np.concatenate([chunks[index] for index in
                   rng.integers(0, len(chunks), len(chunks))]))) for _ in range(300)]
    return np.quantile(means, [0.025, 0.975]).tolist()


def backtest_strategy(rows: list[dict[str, Any]], states: list[dict[str, Any]],
                      forecasts: list[dict[str, Any]], *, cost_bps: float = 10.0) -> dict[str, Any]:
    """先用前半段选网格，再在后半段锁定参数检验；未达门槛绝不启用。"""
    if cost_bps < 0:
        raise ValueError("单边成本不得为负")
    daily = _prepare_rows(rows)
    state_by_date = {item.get("as_of"): item for item in states}
    forecast_by_date = {item.get("as_of"): item for item in forecasts}
    dates = [row["trade_date"] for row in daily if row["trade_date"] in forecast_by_date]
    verified = bool(daily) and all(row.get("pit_verified") is True and row.get("published_at")
                                   and row.get("source_id") and row.get("fetched_at")
                                   and str(row["published_at"])[:10] <= row["trade_date"] for row in daily)
    result = {"status": "research_only", "enabled": False, "entry_enabled": False,
              "exit_enabled": False, "pit_verified": verified, "cost_bps_per_side": cost_bps,
              "selection_rule": "开发段固定评分=扣费净收益超过同风险预算被动基线+最大回撤改善（候选最大回撤减基线最大回撤）；同分选实际单边成交额/成交前净值累计换手较低者；锁定段不参与选参",
              "grid": [], "exit_grid": [], "baseline": {}, "selected": None,
              "selected_exit": None, "reasons": []}
    if len(dates) < 756:
        result["reasons"].append(f"独立预测信号少于756日（现有{len(dates)}日）")
        return result
    pivot = dates[len(dates) // 2]
    index_by_date = {row["trade_date"]: index for index, row in enumerate(daily)}
    baseline_index = [index_by_date[day] for day in dates]
    first, last = baseline_index[0], baseline_index[-1]
    if last + 20 >= len(daily) or not all(row["_execution_valid"] and (index == first or not row.get("gap_before"))
                                               for index, row in enumerate(daily[first:last + 21], start=first)):
        result["reasons"].append("策略区间缺少真实开盘/收盘报价、结束后20日执行价格或存在价格缺口")
        return result
    pivot_index = index_by_date[pivot]
    development = (first - 1, pivot_index - 51)
    holdout = (pivot_index - 1, last + 20)
    if development[1] <= development[0] or not all(row["_execution_valid"] and not row.get("gap_before")
                                                  for row in daily[development[0] + 1:holdout[1] + 1]):
        result["reasons"].append("隔离窗口或真实连续开盘/收盘价格历史不足")
        return result
    baseline_dev = _simulate(daily, state_by_date, forecast_by_date, *development, 0, 3, 20, cost_bps, "baseline")
    baseline_holdout = _simulate(daily, state_by_date, forecast_by_date, *holdout, 0, 3, 20, cost_bps, "baseline")
    result["baseline"] = {"development": _compact(baseline_dev), "holdout": _compact(baseline_holdout),
                          "risk_budget": RISK_BUDGET}
    for level, stable_days, holding in GRID:
        entry_dev = _simulate(daily, state_by_date, forecast_by_date, *development,
                              level, stable_days, holding, cost_bps, "entry")
        entry_holdout = _simulate(daily, state_by_date, forecast_by_date, *holdout,
                                  level, stable_days, holding, cost_bps, "entry")
        exit_dev = _simulate(daily, state_by_date, forecast_by_date, *development,
                             level, stable_days, holding, cost_bps, "exit")
        exit_holdout = _simulate(daily, state_by_date, forecast_by_date, *holdout,
                                 level, stable_days, holding, cost_bps, "exit")
        fields = {"level": level, "confirmation_days": stable_days, "holding_days": holding}
        entry_excess = entry_dev["net_return"] - baseline_dev["net_return"]
        exit_excess = exit_dev["net_return"] - baseline_dev["net_return"]
        entry_drawdown = entry_dev["max_drawdown"] - baseline_dev["max_drawdown"]
        exit_drawdown = exit_dev["max_drawdown"] - baseline_dev["max_drawdown"]
        result["grid"].append({**fields, "development": _compact(entry_dev),
                               "holdout": _compact(entry_holdout),
                               "development_excess": entry_excess,
                               "development_drawdown_improvement": entry_drawdown,
                               "development_selection_score": entry_excess + entry_drawdown,
                               "holdout_excess": entry_holdout["net_return"] - baseline_holdout["net_return"],
                               "holdout_daily_excess": [a - b for a, b in zip(entry_holdout["daily_returns"],
                                                                               baseline_holdout["daily_returns"]) ]})
        result["exit_grid"].append({**fields, "development": _compact(exit_dev),
                                    "holdout": _compact(exit_holdout),
                                    "development_excess": exit_excess,
                                    "development_drawdown_improvement": exit_drawdown,
                                    "development_selection_score": exit_excess + exit_drawdown,
                                    "holdout_excess": exit_holdout["net_return"] - baseline_holdout["net_return"],
                                    "holdout_daily_excess": [a - b for a, b in zip(exit_holdout["daily_returns"],
                                                                                    baseline_holdout["daily_returns"]) ]})
    for name in ("trend", "volatility"):
        result["baseline"][name] = {
            "development": _compact(_simulate(daily, state_by_date, forecast_by_date, *development,
                                               0, 3, 20, cost_bps, name)),
            "holdout": _compact(_simulate(daily, state_by_date, forecast_by_date, *holdout,
                                           0, 3, 20, cost_bps, name))}
    for kind, source, target, flag in (("入场", "grid", "selected", "entry_enabled"),
                                       ("离场", "exit_grid", "selected_exit", "exit_enabled")):
        candidates = sorted(result[source], key=_development_rank, reverse=True)
        if not candidates or candidates[0]["development"]["trades"] < 12:
            result["reasons"].append(f"{kind}开发段成交少于12次")
            continue
        chosen = candidates[0]
        interval = _block_ci(chosen["holdout_daily_excess"])
        result[target] = {key: chosen[key] for key in ("level", "confirmation_days", "holding_days", "holdout", "holdout_excess")}
        result[target]["daily_excess_95ci_block20"] = interval
        minimum_trades = 12 if chosen["holding_days"] == 20 else 20
        result[flag] = (chosen["holdout"]["trades"] >= minimum_trades and
                        chosen["holdout_excess"] > 0 and interval[0] > 0)
        if not result[flag]:
            result["reasons"].append(f"{kind}锁定参数后未超过同区间10%风险预算买入持有或区间下界未超过零")
    if not verified:
        result["reasons"].append("逐行PIT来源或发布时间尚未核实")
    # 模型预测也必须逐日来自时间外评估，不接受整段样本内拟合的预测。
    if not all(forecast_by_date[day].get("walk_forward_oos") is True for day in dates):
        result["reasons"].append("预测不全是逐日时间外结果")
    if not verified or not all(forecast_by_date[day].get("walk_forward_oos") is True for day in dates):
        result["entry_enabled"] = result["exit_enabled"] = False
    if result["entry_enabled"] or result["exit_enabled"]:
        result.update(status="passed", enabled=True)
    return result
