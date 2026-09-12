"""第四版模型的无前视、缺口隔离及发布门槛验证。"""

from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import json
import math
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from scripts.a_share_panic_index.risk_v4 import core, forecast
from scripts.a_share_panic_index.risk_v4.strategy import evaluate_signal
from scripts.a_share_panic_index.risk_v4.portfolio import (
    build_advice, import_csv, validate_portfolio,
)
from scripts.a_share_panic_index.risk_v4.validation import (
    GRID, _confirmed, _development_rank, _simulate, backtest_strategy,
)


def make_rows(count: int, *, verified: bool = True) -> list[dict]:
    rows = []
    day = date(2010, 1, 1)
    for index in range(count):
        # 有涨有跌，保证训练中既有事件也有非事件。
        close = 100.0 * math.exp(0.0001 * index + 0.07 * math.sin(index / 14))
        rows.append({"trade_date": (day + timedelta(days=index)).isoformat(),
                     "open": close * 1.001, "high": close * 1.003,
                     "low": close * 0.997, "close": close,
                     "amount": 1_000_000.0, "published_at": (day + timedelta(days=index)).isoformat(),
                     "fetched_at": "2026-09-12", "pit_verified": verified, "source_id": "test"})
    return rows


class TestCore(unittest.TestCase):
    def test_zero_pressure_and_future_independence(self):
        rows = make_rows(900)
        before = core.compute_state(rows[:850])
        self.assertEqual(before, core.compute_state(rows, as_of=rows[849]["trade_date"]))
        self.assertEqual(before["state"], "ready")
        self.assertEqual(core.compute_history(rows, 51)[0]["as_of"], rows[-51]["trade_date"])
        flat = make_rows(900)
        for row in flat:
            row.update(open=100.0, high=100.0, low=100.0, close=100.0)
        result = core.compute_state(flat)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["overheat"], 0.0)

    def test_gap_breaks_features_without_filling(self):
        rows = make_rows(900)
        rows[850]["gap_before"] = True
        self.assertEqual(core.compute_state(rows[:851])["state"], "insufficient_data")
        self.assertEqual(core.compute_state(rows[:912] if len(rows) > 912 else rows)["state"], "insufficient_data")
        samples, _ = forecast._samples(core._prepare_rows(rows))
        self.assertNotIn(rows[849]["trade_date"], {item["date"] for item in samples})

    def test_equal_weight_and_missing_amount_only_affects_overheat(self):
        rows = make_rows(900)
        state = core.compute_state(rows)
        values = [state["components"][key]["percentile"] for key in core.WEIGHTS]
        self.assertAlmostEqual(state["score"], sum(values) / 3)
        rows[-1]["amount"] = None
        missing_amount = core.compute_state(rows)
        self.assertEqual(missing_amount["state"], "ready")
        self.assertAlmostEqual(missing_amount["score"], state["score"])
        self.assertIsNone(missing_amount["overheat"])
        self.assertTrue(any("amount20_stretch" in item for item in missing_amount["missing"]))

    def test_real_close_only_still_has_identical_score_and_highest_close_drawdown(self):
        rows = make_rows(900)
        complete = core.compute_state(rows)
        closes = [row["close"] for row in rows[-60:]]
        self.assertAlmostEqual(complete["components"]["drawdown"]["raw"],
                               max(0.0, 1.0-rows[-1]["close"]/max(closes)))
        for row in rows:
            row.update(open=None, high=None, low=None)
        prepared = core._prepare_rows(rows)
        self.assertTrue(all(row["_valid"] and not row["_execution_valid"] for row in prepared))
        close_only = core.compute_state(rows)
        self.assertEqual(close_only["state"], "ready")
        self.assertEqual(close_only["score"], complete["score"])
        self.assertEqual(close_only["components"], complete["components"])
        samples, latest = forecast._samples(prepared)
        self.assertTrue(samples)
        self.assertEqual(latest["date"], rows[-1]["trade_date"])

    def test_reference_never_extends_older_than_five_years(self):
        rows = make_rows(900)
        rows[-1]["trade_date"] = "2026-09-12"
        result = core.compute_state(rows)
        self.assertEqual(result["state"], "insufficient_data")
        self.assertTrue(any("当前0日" in message for message in result["missing"]))
        self.assertEqual(core._five_years_before(date(2024, 2, 29)), date(2019, 2, 28))


class TestForecast(unittest.TestCase):
    def test_insufficient_history_and_strict_pit_gate(self):
        result = forecast.train_and_validate(make_rows(300, verified=False), symbol="sh000300")
        self.assertFalse(result["validation"]["publishable"])
        self.assertFalse(result["validation"]["pit_verified"])
        self.assertEqual(result["latest"]["published"], {})

    def test_serialized_walk_forward_artifact_and_probability_monotonicity(self):
        rows = make_rows(380, verified=False)
        fit_calls = []
        original_fit = forecast._fit

        def record_fit(train, calibration, **kwargs):
            fit_calls.append((train[-1]["index"], calibration[0]["index"],
                              calibration[-1]["index"]))
            return original_fit(train, calibration, **kwargs)

        with (patch.object(forecast, "MIN_TRAIN", 80),
              patch.object(forecast, "MIN_CALIBRATION", 50),
              patch.object(forecast, "EMBARGO", 10),
              patch.object(forecast, "MIN_OOS", 50),
              patch.object(forecast, "_fit", side_effect=record_fit)):
            result = forecast.train_and_validate(rows, symbol="sh000300")
        json.dumps(result, allow_nan=False)
        self.assertTrue(result["artifact"]["hazards"])
        self.assertEqual(result["latest"]["state"], "research_only")
        self.assertEqual(result["latest"]["published"], {})
        self.assertTrue(result["oos_predictions"][0]["walk_forward_oos"])
        first_test_index = next(i for i, row in enumerate(rows)
                                if row["trade_date"] == result["oos_predictions"][0]["as_of"])
        self.assertGreaterEqual(fit_calls[0][1] - fit_calls[0][0], 11)
        self.assertGreaterEqual(first_test_index - fit_calls[0][2], 11)
        values = result["latest"]["raw_research"]
        for threshold in forecast.THRESHOLDS:
            self.assertEqual([values[f"{h}d_{threshold}pct"] for h in forecast.HORIZONS],
                             sorted(values[f"{h}d_{threshold}pct"] for h in forecast.HORIZONS))
        for horizon in forecast.HORIZONS:
            self.assertEqual([values[f"{horizon}d_{threshold}pct"] for threshold in forecast.THRESHOLDS],
                             sorted([values[f"{horizon}d_{threshold}pct"] for threshold in forecast.THRESHOLDS], reverse=True))

    def test_event_minimum_and_terminal_quantile_have_distinct_targets(self):
        rows = make_rows(120)
        for index in range(60, 120):
            rows[index].update(open=100.0, high=101.0, low=89.0, close=100.0)
        rows[61].update(open=90.0, high=91.0, low=89.0, close=90.0)
        samples, _ = forecast._samples(core._prepare_rows(rows))
        first = next(item for item in samples if item["date"] == rows[60]["trade_date"])
        self.assertEqual(first["event"]["5d_5pct"], 1)
        self.assertEqual(first["terminal"]["5"], 0.0)
        self.assertAlmostEqual(first["minimum"]["5"], -0.1)

    def test_published_whitelist_does_not_expose_failed_events(self):
        rows = make_rows(380)
        with (patch.object(forecast, "MIN_TRAIN", 80),
              patch.object(forecast, "MIN_CALIBRATION", 50),
              patch.object(forecast, "EMBARGO", 10),
              patch.object(forecast, "MIN_OOS", 50)):
            artifact = forecast.train_and_validate(rows, symbol="sh000300")["artifact"]
        artifact["validation"].update(publishable=True, pit_verified=True,
                                      published_events=["5d_3pct"], published_intervals=["5d"])
        output = forecast.predict(rows, artifact)
        self.assertEqual(list(output["published"]["probabilities"]), ["5d_3pct"])
        self.assertEqual(set(output["published"]["quantiles"]), {"5d_q10", "5d_q50", "5d_q90"})
        self.assertNotIn("20d_5pct", output["published"]["probabilities"])
        old_artifact = dict(artifact)
        del old_artifact["configuration_sha256"]
        with self.assertRaisesRegex(ValueError, "模型配置已经变更"):
            forecast.predict(rows, old_artifact)

    def test_close_only_history_can_train_research_forecast(self):
        rows = make_rows(380, verified=False)
        for row in rows:
            row.update(open=None, high=None, low=None)
        with (patch.object(forecast, "MIN_TRAIN", 80),
              patch.object(forecast, "MIN_CALIBRATION", 50),
              patch.object(forecast, "EMBARGO", 10),
              patch.object(forecast, "MIN_OOS", 50)):
            result = forecast.train_and_validate(rows, symbol="cn931865")
        self.assertTrue(result["artifact"]["hazards"])
        self.assertEqual(result["latest"]["state"], "research_only")
        self.assertFalse(result["validation"]["pit_verified"])
        self.assertIn("simple_volatility_brier", result["validation"]["events"]["20d_5pct"])
        self.assertIn("20d_5pct", result["oos_predictions"][0]["simple_volatility"])

    def test_simple_volatility_baseline_is_fixed_and_uses_only_signal_day_vol(self):
        item = {"x": [0, 0, 0, 0, 0.2]}
        values = forecast._simple_volatility_probabilities([item])
        expected = math.erfc(-math.log(.95)/(0.2/math.sqrt(252)*math.sqrt(40)))
        self.assertAlmostEqual(values["20d_5pct"][0], expected)
        self.assertGreater(values["50d_5pct"][0], values["20d_5pct"][0])
        self.assertLess(values["20d_10pct"][0], values["20d_5pct"][0])
        self.assertEqual(forecast._simple_volatility_probabilities([{"x": [0,0,0,0,0]}])["20d_5pct"][0],0)


class TestStrategy(unittest.TestCase):
    def test_no_operation_without_separate_strategy_validation(self):
        state = {"state": "ready", "score": 96, "overheat": 0, "model_version": "4.0"}
        prediction = {"state": "published", "published": {"probabilities": {"20d_5pct": 0.01}}}
        self.assertEqual(evaluate_signal(state, prediction, {})["action"], "observe")
        self.assertEqual(backtest_strategy(make_rows(100), [], [])["enabled"], False)

    def test_missing_true_open_quote_disables_strategy_without_disabling_market_model(self):
        rows = make_rows(900)
        rows[100].update(open=None, high=None, low=None)
        self.assertEqual(core.compute_state(rows)["state"], "ready")
        pseudo_oos = [{"as_of": row["trade_date"], "walk_forward_oos": True}
                      for row in rows[60:820]]
        result = backtest_strategy(rows, [], pseudo_oos)
        self.assertFalse(result["enabled"])
        self.assertIn("真实开盘", result["reasons"][0])

    def test_stability_confirmation_and_daily_mark_to_market(self):
        rows = core._prepare_rows(make_rows(100))
        for index in range(6):
            rows[index].update(open=100.0 + index, close=100.0 + index)
        states = {row["trade_date"]: {"state": "ready", "score": 95.0, "overheat": 0.0}
                  for row in rows}
        predictions = {row["trade_date"]: {"raw_research": {"20d_5pct": 0.1},
                                             "quantiles": {"20d_q10": -0.02,
                                                           "20d_q50": 0.01}}
                       for row in rows}
        self.assertFalse(_confirmed(1, rows, states, predictions, 85, 3, "entry", 10))
        self.assertTrue(_confirmed(2, rows, states, predictions, 85, 3, "entry", 10))
        states[rows[3]["trade_date"]]["score"] = 60.0
        self.assertFalse(_confirmed(5, rows, states, predictions, 85, 3, "entry", 10))
        states[rows[3]["trade_date"]]["score"] = 95.0
        rows[4]["close"] = rows[3]["close"] - 0.1
        self.assertFalse(_confirmed(5, rows, states, predictions, 85, 3, "entry", 10))
        rows[4]["close"] = rows[3]["close"] + 1.0
        predictions[rows[4]["trade_date"]]["quantiles"]["20d_q50"] = 0.001
        self.assertFalse(_confirmed(5, rows, states, predictions, 85, 3, "entry", 10))
        predictions[rows[4]["trade_date"]]["quantiles"]["20d_q50"] = 0.01
        predictions[rows[4]["trade_date"]]["quantiles"]["20d_q10"] = -0.049
        self.assertFalse(_confirmed(5, rows, states, predictions, 85, 3, "entry", 10))
        predictions[rows[4]["trade_date"]]["quantiles"]["20d_q10"] = -0.02
        result = _simulate(rows, states, predictions, 2, 40, 85, 3, 5, 10.0, "entry")
        self.assertEqual(len(result["daily_nav"]), 38)
        self.assertGreater(result["trades"], 0)
        self.assertGreater(result["turnover"], 0.1)
        self.assertLessEqual(result["max_drawdown"], 0.0)
        self.assertEqual(sorted({item[1] for item in GRID}), [3, 5])

    def test_turnover_counts_both_execution_sides_and_dev_rank_uses_drawdown(self):
        rows = core._prepare_rows(make_rows(60))
        baseline = _simulate(rows, {}, {}, 0, 30, 0, 3, 20, 10.0, "baseline")
        self.assertEqual(baseline["trades"], 0)
        self.assertGreater(baseline["turnover"], .19)
        self.assertLess(baseline["turnover"], .22)
        higher_return_deeper_dd = {"development": {"trades": 12, "turnover": .5},
                                   "development_selection_score": .01}
        lower_return_better_dd = {"development": {"trades": 12, "turnover": .7},
                                  "development_selection_score": .02}
        self.assertGreater(_development_rank(lower_return_better_dd),
                           _development_rank(higher_return_deeper_dd))
        lower_turnover = {"development": {"trades": 12, "turnover": .3},
                          "development_selection_score": .02}
        self.assertGreater(_development_rank(lower_turnover),
                           _development_rank(lower_return_better_dd))

    def test_style_does_not_bypass_selected_strategy_parameters(self):
        rows = make_rows(5)
        states = [{"state": "ready", "as_of": row["trade_date"], "score": 95.0,
                   "overheat": 95.0, "model_version": "4.0"} for row in rows]
        predictions = [{"state": "published", "as_of": row["trade_date"],
                        "published": {"probabilities": {"20d_5pct": 0.1},
                                      "quantiles": {"20d_q10": -0.02, "20d_q50": 0.01,
                                                    "20d_q90": 0.1}}} for row in rows]
        context = {"style": "balanced", "recent_states": states,
                   "recent_forecasts": predictions, "recent_closes": [100, 101, 102, 103, 104],
                   "live_observation_days": 20,
                   "strategy_validation": {"status": "passed", "enabled": True,
                                           "entry_enabled": True, "selected": {
                                               "level": 85, "confirmation_days": 5}}}
        self.assertEqual(evaluate_signal(states[-1], predictions[-1], context)["action"], "review_entry")
        context["strategy_validation"]["selected"]["level"] = 75
        self.assertEqual(evaluate_signal(states[-1], predictions[-1], context)["action"], "observe")

    def test_exit_uses_same_probability_and_overheat_rule_without_entry_quantiles(self):
        rows = make_rows(5)
        states = [{"state": "ready", "as_of": row["trade_date"], "score": 30.0,
                   "overheat": 90.0, "model_version": "4.0"} for row in rows]
        predictions = [{"state": "published", "as_of": row["trade_date"],
                        "published": {"probabilities": {"20d_5pct": 0.3}}} for row in rows]
        context = {"style": "balanced", "has_position": True,
                   "recent_states": states, "recent_forecasts": predictions,
                   "recent_closes": [100, 99, 98, 97, 96], "live_observation_days": 20,
                   "strategy_validation": {"status": "passed", "enabled": True,
                                           "exit_enabled": True, "cost_bps_per_side": 10,
                                           "selected_exit": {"level": 85, "confirmation_days": 3}}}
        self.assertEqual(evaluate_signal(states[-1], predictions[-1], context)["action"], "review_exit")
        prepared = core._prepare_rows(rows)
        states_by_date = {item["as_of"]: item for item in states}
        research_by_date = {item["as_of"]: {"raw_research": {"20d_5pct": 0.3}}
                            for item in predictions}
        self.assertTrue(_confirmed(4, prepared, states_by_date, research_by_date, 85, 3, "exit", 10))


def fund_context(code: str, *, action: str = "review_entry", industry: str = "科技") -> dict:
    as_of = date(2026, 9, 12)
    returns = [{"trade_date": (as_of - timedelta(days=269 - index)).isoformat(),
                "return": 0.002 * math.sin(index / (9 if code.endswith("1") else 11)),
                "segment": "verified", "gap_before": False} for index in range(270)]
    return {"name": code, "symbol": "sh000300", "asset_class": "equity",
            "industry": industry, "nav": 1.0, "nav_date": as_of.isoformat(),
            "returns": returns, "state": "ready", "observation_ready": True,
            "fees_verified": True, "calendar_verified": False,
            "forecast": {"state": "published", "published": {
                "probabilities": {"20d_5pct": 0.1},
                "quantiles": {"20d_q10": -0.02, "20d_q50": 0.01}},
                "validation": {"publishable": True,
                               "events": {"20d_5pct": {"passed": True}},
                               "intervals": {"20d": {"passed": True}}}},
            "strategy": {"enabled": True, "status": "passed", "entry_enabled": True,
                         "exit_enabled": True,
                         "signal": {"eligible": True, "action": action}}}


def fund_lot(code: str, value: float) -> dict:
    return {"id": "lot-" + code, "code": code, "shares": value,
            "valuation_date": "2026-09-12", "confirmed_date": "2026-09-11",
            "fee_buy": 0.01, "fee_sell": 0.01, "in_transit": 0}


class TestPortfolio(unittest.TestCase):
    def test_minimal_chinese_csv_and_duplicate_identity(self):
        csv_result = import_csv("基金代码,持有市值,估值日期\n013273,1000,2026-09-11\n")
        self.assertTrue(csv_result["valid"])
        self.assertEqual(csv_result["portfolio"]["lots"][0]["in_transit"], 0)
        duplicate = validate_portfolio({"version": 1, "cash": 1,
                                        "lots": [fund_lot("013271", 100),
                                                 dict(fund_lot("013272", 100), id="lot-013271")]})
        self.assertFalse(duplicate["valid"])
        self.assertTrue(any("ID 重复" in item["message"] for item in duplicate["errors"]))

    def test_joint_feasible_targets_and_global_gross_cap(self):
        portfolio = {"version": 1, "cash": 800,
                     "lots": [fund_lot("013271", 100), fund_lot("013272", 100)]}
        contexts = {"013271": fund_context("013271", industry="科技"),
                    "013272": fund_context("013272", industry="医药")}
        result = build_advice(portfolio, contexts, as_of="2026-09-12")
        self.assertTrue(result["target_feasible"], result["warnings"])
        self.assertEqual(len(result["advice"]), 2)
        self.assertTrue(all(item["delta_weight"] >= result["constraints"]["no_trade_band"] - 1e-7
                            for item in result["advice"]))
        self.assertLessEqual(sum(abs(item["delta_weight"]) for item in result["advice"]), 0.1000001)
        self.assertLessEqual(sum(item["target_weight"] for item in result["positions"]), 1.0)
        self.assertLessEqual(result["risk"]["target_annualized_volatility"], result["constraints"]["vol_target"])
        self.assertFalse(result["exact_amount_available"])

    def test_sale_proceeds_cannot_finance_same_day_buy(self):
        portfolio = {"version": 1, "cash": 40,
                     "constraints": {"single_fund_cap": 1, "equity_cap": 1, "industry_cap": 1,
                                     "vol_target": 1},
                     "lots": [fund_lot("013271", 100), fund_lot("013272", 860)]}
        contexts = {"013271": fund_context("013271", industry="科技"),
                    "013272": fund_context("013272", action="review_exit", industry="医药")}
        result = build_advice(portfolio, contexts, as_of="2026-09-12")
        self.assertFalse(result["target_feasible"])
        self.assertEqual(result["advice"], [])
        self.assertTrue(all(item["target_weight"] is None for item in result["positions"]))
        self.assertTrue(result["risk"]["available"])
        self.assertFalse(result["exact_amount_available"])

    def test_unverified_strategy_or_future_nav_never_makes_action(self):
        portfolio = {"version": 1, "cash": 900, "lots": [fund_lot("013271", 100)]}
        context = fund_context("013271")
        context["observation_ready"] = False
        unverified = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        self.assertEqual(unverified["advice"], [])
        self.assertFalse(unverified["exact_amount_available"])
        context["observation_ready"] = True
        context["forecast"]["validation"]["publishable"] = False
        unpublished = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        self.assertFalse(unpublished["target_feasible"])
        self.assertEqual(unpublished["advice"], [])
        context["forecast"]["validation"]["publishable"] = True
        context["nav_date"] = "2026-09-13"
        future = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        self.assertFalse(future["target_feasible"])
        self.assertIsNone(future["positions"][0]["target_weight"])

    def test_stale_real_nav_remains_reference_value_but_blocks_targets(self):
        portfolio = {"version": 1, "cash": 900, "lots": [fund_lot("013271", 100)]}
        context = fund_context("013271")
        context["nav_date"] = "2026-08-31"
        result = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        self.assertEqual(result["summary"]["total_assets"], 1000)
        self.assertFalse(result["target_feasible"])
        self.assertEqual(result["advice"], [])
        self.assertIn("超过七个自然日", result["positions"][0]["reason"])

    def test_covariance_rejects_future_and_cross_segment_history(self):
        portfolio = {"version": 1, "cash": 900, "lots": [fund_lot("013271", 100)]}
        context = fund_context("013271")
        current = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        context["returns"].append({"trade_date": "2026-09-13", "return": 0.9,
                                   "segment": "verified"})
        future_filtered = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        self.assertEqual(current["risk"]["annualized_volatility"],
                         future_filtered["risk"]["annualized_volatility"])
        context["returns"][150]["gap_before"] = True
        broken = build_advice(portfolio, {"013271": context}, as_of="2026-09-12")
        self.assertFalse(broken["target_feasible"])
        self.assertFalse(broken["risk"]["available"])

    def test_three_buys_cannot_exceed_single_day_global_turnover(self):
        codes = ("013271", "013272", "013273")
        portfolio = {"version": 1, "cash": 700,
                     "lots": [fund_lot(code, 100) for code in codes]}
        contexts = {code: fund_context(code, industry=str(index))
                    for index, code in enumerate(codes)}
        result = build_advice(portfolio, contexts, as_of="2026-09-12")
        self.assertFalse(result["target_feasible"])
        self.assertEqual(result["advice"], [])
        self.assertTrue(all(item["target_weight"] is None for item in result["positions"]))

    def test_fixed_bond_risk_blocks_equity_buy_when_total_vol_exceeds_cap(self):
        portfolio = {"version": 1, "cash": 400,
                     "constraints": {"single_fund_cap": 0.8, "vol_target": 0.06},
                     "lots": [fund_lot("013271", 500), fund_lot("013272", 100)]}
        bond = fund_context("013271", industry="")
        bond["asset_class"] = "bond"
        bond["observation_ready"] = False
        for index, row in enumerate(bond["returns"]):
            row["return"] = 0.05 * math.sin(index / 3)
        equity = fund_context("013272", industry="科技")
        result = build_advice(portfolio, {"013271": bond, "013272": equity},
                              as_of="2026-09-12")
        self.assertTrue(result["risk"]["available"])
        self.assertGreater(result["risk"]["annualized_volatility"], result["constraints"]["vol_target"])
        self.assertFalse(result["target_feasible"])


if __name__ == "__main__":
    unittest.main()
