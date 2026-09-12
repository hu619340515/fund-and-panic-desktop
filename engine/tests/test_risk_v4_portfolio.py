from __future__ import annotations

try:
    import _bootstrap
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import unittest
from unittest.mock import patch

import pandas as pd
from scripts.a_share_panic_index.risk_v4 import nav, portfolio


def _lot(**overrides):
    value = {
        "id": "lot-1",
        "code": "000001",
        "shares": 100.0,
        "market_value": None,
        "valuation_date": "2026-09-11",
        "confirmed_date": "2026-01-01",
        "fee_buy": 0.001,
        "fee_sell": 0.005,
        "in_transit": 0.0,
        "baseline_weight": None,
        "industry": None,
    }
    value.update(overrides)
    return value


class TestPortfolio(unittest.TestCase):
    def test_validate_keeps_six_digit_code_and_profile_defaults(self):
        result = portfolio.validate_portfolio({"cash": 20, "profile": "conservative", "lots": [_lot(code="000001")]})
        self.assertTrue(result["valid"])
        self.assertEqual(result["portfolio"]["lots"][0]["code"], "000001")
        self.assertEqual(result["portfolio"]["constraints"]["equity_cap"], 0.60)
        self.assertEqual(result["portfolio"]["constraints"]["vol_target"], 0.06)

    def test_import_csv_requires_chinese_headers_and_preserves_code(self):
        text = "基金代码,持有份额,持有市值,估值日期,申购确认日期,基准权重,行业,申购费率,赎回费率,在途金额\n000001,10,,2026-09-11,,0.1,,0.001,0.005,0\n"
        result = portfolio.import_csv(text)
        self.assertTrue(result["valid"])
        self.assertEqual(result["portfolio"]["lots"][0]["code"], "000001")
        self.assertFalse(portfolio.import_csv("code,估值日期\n000001,2026-09-11\n")["valid"])

    def test_unverified_context_has_no_trade_instruction_or_exact_amount(self):
        advice = portfolio.build_advice(
            {"cash": 100, "profile": "balanced", "lots": [_lot()]},
            {"000001": {"nav": 1.2, "nav_date": "2026-09-11", "state": "unverified", "returns": []}},
            as_of="2026-09-11",
        )
        item = advice["positions"][0]
        self.assertEqual(item["action"], "observe")
        self.assertIn("策略、概率或20日实盘观察未通过", item["reason"])
        self.assertFalse(advice["exact_amount_available"])
        self.assertFalse(advice["risk"]["available"])

    def test_risk_needs_252_shared_returns(self):
        returns = [{"trade_date": f"2025-01-{day:02d}", "return": 0.01} for day in range(1, 20)]
        advice = portfolio.build_advice(
            {"cash": 0, "lots": [_lot()]},
            {"000001": {"nav": 1.0, "nav_date": "2026-09-11", "returns": returns}},
            as_of="2026-09-11",
        )
        self.assertFalse(advice["risk"]["available"])
        self.assertIn("252", advice["risk"]["reason"])

    def test_target_range_never_reverses_when_baseline_exceeds_cap(self):
        advice = portfolio.build_advice(
            {"cash": 0, "lots": [_lot()]},
            {"000001": {"nav": 1.0, "nav_date": "2026-09-11", "returns": []}},
            as_of="2026-09-11",
        )
        item = advice["positions"][0]
        self.assertIsNone(item["target_min"])
        self.assertIsNone(item["target_max"])
        self.assertFalse(advice["target_feasible"])


class _Ak:
    @staticmethod
    def fund_open_fund_info_em(**kwargs):
        if kwargs["indicator"] in {"分红送配详情", "拆分详情"}:
            return pd.DataFrame()
        return pd.DataFrame({"净值日期": ["2026-09-10", "2026-09-11"], "单位净值": [1.1, 1.2], "日增长率": ["1%", "1%"]})

    @staticmethod
    def fund_individual_basic_info_xq(**_kwargs):
        return pd.DataFrame({"item": ["基金名称", "基金类型", "业绩比较基准"], "value": ["示例主动基金", "混合型", "沪深300收益率"]})


class TestFundNav(unittest.TestCase):
    @patch.object(nav, "_load_akshare", return_value=_Ak)
    def test_history_uses_unit_nav_and_never_accumulates_pct(self, _loader):
        result = nav.fetch_fund_history("000001")
        self.assertTrue(result["available"])
        self.assertEqual(result["history"][-1]["unit_nav"], 1.2)
        self.assertTrue(result["total_return"]["available"])
        self.assertTrue(result["completeness"]["total_return_verified"])
        self.assertEqual(result["events"]["distributions"]["events"], [])

    @patch.object(nav, "_load_akshare", return_value=_Ak)
    def test_metadata_does_not_guess_symbol_or_industry(self, _loader):
        result = nav.fetch_fund_metadata("000001")
        self.assertFalse(result["verified"])
        self.assertIsNone(result["symbol"])
        self.assertEqual(result["industry_candidates"], [])

    def test_total_return_uses_cash_distribution_instead_of_pct(self):
        result = nav.compute_total_returns(
            [{"trade_date": "2026-01-01", "unit_nav": 1.0}, {"trade_date": "2026-01-02", "unit_nav": 0.9}],
            {"available": True, "events": [{"effective_date": "2026-01-02", "cash_per_unit": 0.2}], "unresolved": []},
            {"available": True, "events": [], "unresolved": []},
        )
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["series"][-1]["total_return"], 1.1)

    def test_unresolved_event_only_splits_the_affected_segment(self):
        result = nav.compute_total_returns(
            [
                {"trade_date": "2026-01-01", "unit_nav": 1.0},
                {"trade_date": "2026-01-02", "unit_nav": 1.0},
                {"trade_date": "2026-01-03", "unit_nav": 1.1},
            ],
            {"available": True, "events": [], "unresolved": [{"effective_date": "2026-01-02", "reason": "金额不明"}]},
            {"available": True, "events": [], "unresolved": []},
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["series"][-1]["segment"], 1)


if __name__ == "__main__":
    unittest.main()
