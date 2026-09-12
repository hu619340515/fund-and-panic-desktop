"""基金事件复权与基准暴露的真实逐日语义测试。"""

from __future__ import annotations

try:
    import _bootstrap
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import math
import unittest
from datetime import date, timedelta
import pandas as pd

from scripts.a_share_panic_index.risk_v4 import exposure, nav


def _source(events=()):
    return {"available": True, "events": list(events), "unresolved": []}


class FundReturnTests(unittest.TestCase):
    def test_ten_units_distribution_extracts_cash_not_unit_count(self):
        event = nav._parse_distribution({"除息日": "2026-01-02", "分红方案": "每10份派现金1.3元"})
        self.assertTrue(event["valid"])
        self.assertAlmostEqual(event["cash_per_unit"], .13)
        result = nav.compute_total_returns(
            [{"trade_date": "2026-01-01", "unit_nav": 1.0},
             {"trade_date": "2026-01-02", "unit_nav": .87}],
            _source([{k: v for k, v in event.items() if k != "valid"}]), _source(),
        )
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["series"][-1]["return"], 0)
        self.assertEqual(result["series"][-1]["period_start"], "2026-01-01")

    def test_actual_eastmoney_ten_unit_dividend_field_and_per_unit_variants(self):
        source_events = (
            ({"年份": "2024年", "权益登记日": "2024-11-11", "除息日": "2024-11-11",
              "每10份分红": "每10份派现金0.1600元", "分红发放日": "2024-11-12"}, .016),
            ({"年份": "2021年", "权益登记日": "2021-09-07", "除息日": "2021-09-07",
              "每10份分红": "每10份派现金0.1200元", "分红发放日": "2021-09-08"}, .012),
            ({"除息日": "2026-01-02", "每份派现金": "0.13元"}, .13),
            ({"除息日": "2026-01-02", "每10份派现金": "每10份派现金1.3元"}, .13),
        )
        for raw, expected in source_events:
            with self.subTest(raw=raw):
                event = nav._parse_distribution(raw)
                self.assertTrue(event["valid"])
                self.assertAlmostEqual(event["cash_per_unit"], expected)
        self.assertIsNone(nav._distribution_cash({"每10份分红": "每10份派现金"}))

    def test_payout_or_record_date_cannot_masquerade_as_ex_date(self):
        for key in ("权益登记日", "分红发放日", "日期"):
            with self.subTest(key=key):
                event = nav._parse_distribution({key: "2026-01-02", "每10份派现金": "1.3"})
                self.assertFalse(event["valid"])
                self.assertIsNone(event["effective_date"])
        self.assertFalse(nav._parse_split({"日期": "2026-01-02", "拆分比例": "1:2"})["valid"])

    def test_conflicting_duplicate_nav_is_not_silently_overwritten(self):
        rows, diagnostics = nav._unit_nav_rows(pd.DataFrame({
            "净值日期": ["2026-01-01", "2026-01-02", "2026-01-02"],
            "单位净值": [1.0, 1.0, .8],
        }))
        self.assertEqual(rows, [])
        self.assertIn("互相冲突", diagnostics[0])

    def test_event_on_missing_nav_date_breaks_return_interval(self):
        result = nav.compute_total_returns(
            [{"trade_date": "2026-01-01", "unit_nav": 1.0},
             {"trade_date": "2026-01-03", "unit_nav": .87},
             {"trade_date": "2026-01-04", "unit_nav": .90}],
            _source([{"effective_date": "2026-01-02", "cash_per_unit": .13}]), _source(),
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["series"][1]["gap_before"])
        self.assertNotIn("return", result["series"][1])
        self.assertAlmostEqual(result["series"][2]["return"], .90/.87-1)

    def test_simultaneous_split_and_cash_does_not_assume_event_order(self):
        result = nav.compute_total_returns(
            [{"trade_date": "2026-01-01", "unit_nav": 1.0},
             {"trade_date": "2026-01-02", "unit_nav": .5}],
            _source([{"effective_date": "2026-01-02", "cash_per_unit": .1}]),
            _source([{"effective_date": "2026-01-02", "ratio": 2}]),
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["series"][-1]["gap_before"])
        repeated = nav.compute_total_returns(
            [{"trade_date": "2026-01-01", "unit_nav": 1.0},
             {"trade_date": "2026-01-02", "unit_nav": .9}],
            _source([{"effective_date": "2026-01-02", "cash_per_unit": .1}] * 2), _source(),
        )
        self.assertFalse(repeated["available"])


class FundExposureTests(unittest.TestCase):
    @staticmethod
    def _prices(count=255):
        days=[]
        current=date(2025, 1, 1)
        while len(days)<count:
            if current.weekday()<5:
                days.append(current.isoformat())
            current+=timedelta(days=1)
        close=100.0
        bars=[]
        fund=[]
        for index,day in enumerate(days):
            change=.001 + .009*math.sin(index*0.13)
            close*=1+change
            bars.append({"trade_date": day, "close": close, "gap_before": False})
            if index:
                fund.append({"trade_date": day, "period_start": days[index-1],
                             "return": change*1.05, "segment": 0})
        return bars,fund

    def test_252_actual_adjacent_days_can_establish_exposure(self):
        bars,fund=self._prices()
        result=exposure.check_exposure(fund,bars,as_of=bars[-1]["trade_date"])
        self.assertTrue(result["stable"])
        self.assertEqual(result["observations"],252)

    def test_missing_fund_day_does_not_hide_inside_252_intersection(self):
        bars,fund=self._prices(260)
        missing=bars[-125]["trade_date"]
        fund=[row for row in fund if row["trade_date"]!=missing]
        result=exposure.check_exposure(fund,bars,as_of=bars[-1]["trade_date"])
        self.assertFalse(result["available"])
        self.assertIn("缺失净值",result["reason"])

    def test_cross_period_return_and_segment_change_are_rejected(self):
        bars,fund=self._prices()
        fund[-70]["period_start"]=fund[-71]["period_start"]
        self.assertFalse(exposure.check_exposure(fund,bars,as_of=bars[-1]["trade_date"])["available"])
        bars,fund=self._prices()
        fund[-70]["segment"]=1
        self.assertFalse(exposure.check_exposure(fund,bars,as_of=bars[-1]["trade_date"])["available"])

    def test_old_exposure_window_cannot_authorize_current_mapping(self):
        bars,fund=self._prices()
        future=(date.fromisoformat(bars[-1]["trade_date"])+timedelta(days=30)).isoformat()
        result=exposure.check_exposure(fund,bars,as_of=future)
        self.assertFalse(result["stable"])
        self.assertIn("距离决策日",result["reason"])


if __name__ == "__main__":
    unittest.main()
