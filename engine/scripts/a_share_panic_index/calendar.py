"""上交所交易日、交易阶段和盘中桶计算。"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import exchange_calendars as exchange_calendar
import pandas as pd

from .models import MarketContext


class TradingCalendar:
    def __init__(self, name: str = "XSHG", timezone: str = "Asia/Shanghai"):
        self.calendar = exchange_calendar.get_calendar(name)
        self.timezone = ZoneInfo(timezone)

    def normalize(self, value: datetime | None = None) -> datetime:
        current = value or datetime.now(self.timezone)
        if current.tzinfo is None:
            return current.replace(tzinfo=self.timezone)
        return current.astimezone(self.timezone)

    def is_session(self, value: date) -> bool:
        return bool(self.calendar.is_session(pd.Timestamp(value)))

    def previous_session(self, value: date) -> date:
        timestamp = pd.Timestamp(value)
        if self.calendar.is_session(timestamp):
            return self.calendar.previous_session(timestamp).date()
        return self.calendar.date_to_session(timestamp, direction="previous").date()

    def next_session(self, value: date) -> date:
        timestamp = pd.Timestamp(value)
        if self.calendar.is_session(timestamp):
            return self.calendar.next_session(timestamp).date()
        return self.calendar.date_to_session(timestamp, direction="next").date()

    def sessions_in_range(self, start: date, end: date) -> list[date]:
        return [item.date() for item in self.calendar.sessions_in_range(start, end)]

    def phase(self, value: datetime) -> str:
        current = self.normalize(value)
        if not self.is_session(current.date()):
            return "non_trading_day"
        clock = current.time().replace(tzinfo=None)
        if time(9, 15) <= clock < time(9, 30):
            return "pre_open"
        if time(9, 30) <= clock <= time(11, 30):
            return "morning_session"
        if time(11, 30) < clock < time(13, 0):
            return "lunch_break"
        if time(13, 0) <= clock <= time(15, 0):
            return "afternoon_session"
        if time(15, 0) < clock < time(15, 10):
            return "finalizing"
        if clock >= time(15, 10):
            return "closed_final"
        return "closed"

    def session_minute(self, value: datetime) -> int | None:
        current = self.normalize(value)
        phase = self.phase(current)
        clock = current.time().replace(tzinfo=None)
        if phase == "morning_session":
            return (clock.hour * 60 + clock.minute) - (9 * 60 + 30)
        if phase == "lunch_break":
            return 120
        if phase in {"afternoon_session", "finalizing", "closed_final"}:
            if phase != "afternoon_session" or clock >= time(15, 0):
                return 241
            return 121 + (clock.hour * 60 + clock.minute) - (13 * 60)
        return None

    def session_bucket_5m(self, value: datetime) -> int | None:
        minute = self.session_minute(value)
        return None if minute is None else min(minute // 5, 48)

    def context(
        self,
        now: datetime | None = None,
        requested_date: date | None = None,
    ) -> MarketContext:
        current = self.normalize(now)
        target = requested_date or current.date()
        if target > current.date():
            raise ValueError("不能请求未来日期")
        is_trading = self.is_session(target)
        if not is_trading:
            expected = self.previous_session(target)
            phase = "non_trading_day"
            minute = None
            bucket = None
        elif target != current.date():
            expected = target
            phase = "closed_final"
            minute = 241
            bucket = 48
        else:
            expected = target
            phase = self.phase(current)
            minute = self.session_minute(current)
            bucket = self.session_bucket_5m(current)
        return MarketContext(
            now=current,
            requested_date=target,
            expected_trade_date=expected,
            is_trading_day=is_trading,
            phase=phase,
            session_minute=minute,
            bucket_5m=bucket,
        )
