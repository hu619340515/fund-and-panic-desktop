from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scripts.a_share_panic_index.calendar import TradingCalendar


class TestTradingCalendar(unittest.TestCase):
    def setUp(self):
        self.calendar = TradingCalendar("XSHG", "Asia/Shanghai")
        self.timezone = ZoneInfo("Asia/Shanghai")

    def stamp(self, hour: int, minute: int) -> datetime:
        return datetime(2026, 7, 24, hour, minute, tzinfo=self.timezone)

    def test_session_boundaries_and_lunch_freeze(self):
        self.assertEqual(self.calendar.phase(self.stamp(9, 29)), "pre_open")
        self.assertEqual(self.calendar.session_minute(self.stamp(9, 30)), 0)
        self.assertEqual(self.calendar.session_minute(self.stamp(11, 30)), 120)
        self.assertEqual(self.calendar.phase(self.stamp(11, 31)), "lunch_break")
        self.assertEqual(self.calendar.session_minute(self.stamp(12, 59)), 120)
        self.assertEqual(self.calendar.session_minute(self.stamp(13, 0)), 121)
        self.assertEqual(self.calendar.session_minute(self.stamp(15, 0)), 241)
        self.assertEqual(self.calendar.phase(self.stamp(15, 5)), "finalizing")
        self.assertEqual(self.calendar.phase(self.stamp(15, 10)), "closed_final")

    def test_five_minute_bucket_is_continuous_across_lunch(self):
        self.assertEqual(self.calendar.session_bucket_5m(self.stamp(11, 30)), 24)
        self.assertEqual(self.calendar.session_bucket_5m(self.stamp(12, 30)), 24)
        self.assertEqual(self.calendar.session_bucket_5m(self.stamp(13, 0)), 24)
        self.assertEqual(self.calendar.session_bucket_5m(self.stamp(13, 5)), 25)

    def test_weekend_and_holiday_are_not_sessions(self):
        self.assertFalse(self.calendar.is_session(date(2026, 7, 25)))
        self.assertFalse(self.calendar.is_session(date(2026, 10, 1)))
        context = self.calendar.context(
            datetime(2026, 7, 26, 10, 0, tzinfo=self.timezone)
        )
        self.assertEqual(context.phase, "non_trading_day")
        self.assertEqual(context.expected_trade_date, date(2026, 7, 24))

    def test_future_requested_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "未来日期"):
            self.calendar.context(self.stamp(10, 0), date(2026, 7, 25))


if __name__ == "__main__":
    unittest.main()
