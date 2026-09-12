"""公开基金申赎状态与国内标准基金下次可提交日期；不估算成交净值。"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SOURCE_URL = "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx"
SOURCE_PAGE = "https://fund.eastmoney.com/Fund_sgzt_bzdm.html#fcode,asc_1"
TIMEZONE = ZoneInfo("Asia/Shanghai")
_FIELDS = ("序号", "基金代码", "基金简称", "基金类型", "最新净值/万份收益", "最新净值报告时间",
           "申购状态", "赎回状态", "下一开放日", "购买起点", "日累计限定金额", "预留1", "预留2", "手续费")
_DOMESTIC = ("股票", "混合", "债券", "货币", "指数", "FOF", "基金中基金")
_OTHER_CALENDAR = ("QDII", "海外", "跨境", "港股", "沪港深", "互认", "原油", "黄金", "商品", "REIT", "LOF", "场内")
_OPEN_BUY = {"开放申购", "正常申购", "可申购"}
_CLOSED_BUY = {"暂停申购", "停止申购", "关闭申购", "封闭申购"}
_OPEN_SELL = {"开放赎回", "正常赎回", "可赎回"}
_CLOSED_SELL = {"暂停赎回", "停止赎回", "关闭赎回", "封闭赎回"}


def fetch_execution_terms(code: str, metadata: dict | None, now: datetime | None = None) -> dict:
    """核对下一**申报/提交**日；公开列表没有确认天数及未来成交净值。"""
    current = _current(now)
    stamp = current.isoformat()
    source = {"provider": "eastmoney", "dataset": "基金申购状态", "url": SOURCE_URL,
              "public_page": SOURCE_PAGE, "adapter": "akshare.fund_purchase_em schema"}
    result = {"verified": False, "state": "unverified", "next_order_date": None,
              "next_subscription_date": None, "next_redemption_date": None,
              "subscription_verified": False, "redemption_verified": False,
              "subscription_open": None, "redemption_open": None, "confirmation_days": None,
              "reason": "", "missing": ["确认天数", "未来成交净值", "赎回到账日期"],
              "source": source, "fetched_at": stamp, "raw_status": {}}
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        result["reason"] = "基金代码必须为六位数字字符串"
        return result
    try:
        rows, digest = _request_table()
    except Exception as error:  # noqa: BLE001 - 上游错误不能变成可交易状态
        result["reason"] = f"公开申赎状态获取或解析失败：{error}"
        return result
    result["source"] = dict(source, response_sha256=digest)
    matches = [row for row in rows if len(row) == 13 and str(row[0]).strip().zfill(6) == code]
    if len(matches) != 1:
        result["reason"] = "公开申赎状态未找到唯一对应基金代码"
        return result
    # AKShare fund_purchase_em：原始 datas 有13列；reset_index 后申购/赎回为第6/7列。
    raw = dict(zip(_FIELDS[1:], matches[0]))
    buy_text, sell_text = str(raw["申购状态"] or "").strip(), str(raw["赎回状态"] or "").strip()
    source_next = _date_text(raw["下一开放日"])
    source_limit = _positive_number(raw["日累计限定金额"])
    result["raw_status"] = {"subscription": buy_text, "redemption": sell_text,
                            "source_next_open_date": source_next,
                            "daily_subscription_limit": source_limit,
                            "source_fund_type": str(raw["基金类型"] or "").strip()}
    metadata = metadata if isinstance(metadata, dict) else {}
    kinds = " ".join((str(metadata.get("fund_type") or ""),
                      str(metadata.get("name") or ""), str(raw["基金类型"] or ""),
                      str(raw["基金简称"] or ""))).upper()
    # ETF 联接为场外申购基金；ETF 本身可能涉及交易所申赎，不套用15点规则。
    if (("ETF" in kinds and "联接" not in kinds) or
            any(token.upper() in kinds for token in _OTHER_CALENDAR)):
        result["reason"] = "QDII、跨境或场内等基金的开放日/截止时点不适用国内标准基金日历"
        result["missing"].append("基金适用交易日历与申赎规则")
        return result
    if not any(token.upper() in kinds for token in _DOMESTIC):
        result["reason"] = "基金类型不足以核实国内标准开放式基金的申报日历"
        result["missing"].append("基金适用交易日历与申赎规则")
        return result
    buy = _status(buy_text, _OPEN_BUY, _CLOSED_BUY)
    sell = _status(sell_text, _OPEN_SELL, _CLOSED_SELL)
    result.update(subscription_open=buy, redemption_open=sell)
    if buy is None or sell is None:
        result["missing"].append("限额可用余额或申赎状态含义")
    if buy is None and sell is None:
        result["state"] = "limited" if "限" in buy_text+sell_text else "unverified"
        result["reason"] = "申购及赎回状态都含限额或未知含义，未核实可提交交易"
        return result
    try:
        order_day = _next_xshg_submission(current)
    except Exception as error:  # noqa: BLE001 - 日历不可用须保守关闭
        result["reason"] = f"交易日历未核实：{error}"
        result["missing"].append("下个国内交易日")
        return result
    if source_next and date.fromisoformat(source_next) > current.date():
        # 来源可注明将来的开放日，但不能用周末/非交易日或超远未来的日期猜订单。
        if date.fromisoformat(source_next) > current.date() + timedelta(days=45):
            result["reason"] = "来源下一开放日超过已核实的交易日历范围"
            return result
        if not _is_xshg_session(source_next, current):
            result["reason"] = "来源下一开放日与国内交易日历不一致"
            return result
        order_day = max(order_day, source_next)
    result["verified"] = True
    result["subscription_verified"] = buy is not None
    result["redemption_verified"] = sell is not None
    result["state"] = ("limited" if buy is None or sell is None else
                       "open" if buy and sell else "partially_open" if buy or sell else "closed")
    if buy and source_limit is not None:
        result["state"] += "_with_limit"
        result["missing"].append("日累计限额剩余及订单金额匹配")
    result["next_subscription_date"] = order_day if buy else None
    result["next_redemption_date"] = order_day if sell else None
    result["next_order_date"] = order_day if buy or sell else None
    result["reason"] = ("仅核实另一侧开放状态和提交日期；含限额一侧仍未知，确认天数、成交净值与到账日未知" if buy is None or sell is None else
                        "仅核实当前开放状态和下次可提交日期；限额余额、确认天数、成交净值与到账日未知" if buy and source_limit is not None else
                        "仅核实当前开放状态和下次可提交日期；确认天数、成交净值与到账日未知"
                        if buy or sell else "公开源当前暂停申购及赎回；后续开放状态需再次核验")
    return result


def _current(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(TIMEZONE)
    if not isinstance(now, datetime):
        raise TypeError("now 必须为 datetime")
    return now.replace(tzinfo=TIMEZONE) if now.tzinfo is None else now.astimezone(TIMEZONE)


def _request_table() -> tuple[list[list], str]:
    """与本地 AKShare fund_purchase_em 同一数据接口，额外加入超时和响应尺寸上限。"""
    import requests
    from akshare.utils import demjson

    started = time.monotonic()
    response = requests.get(SOURCE_URL, params={"t": "8", "page": "1,50000", "js": "reData", "sort": "fcode,asc"},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=(4, 12), stream=True)
    try:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            if time.monotonic()-started > 25:
                raise TimeoutError("公开申赎状态读取超过25秒")
            size += len(chunk)
            if size > 24_000_000:
                raise ValueError("公开申赎状态响应过大")
            chunks.append(chunk)
        payload = b"".join(chunks)
    finally:
        response.close()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("gb18030")
    prefix = "var reData="
    normalized = text.strip().removesuffix(";").strip()
    if not normalized.startswith(prefix):
        raise ValueError("申赎状态响应未包含预期的 reData 赋值")
    decoded = demjson.decode(normalized[len(prefix):])
    rows = decoded.get("datas") if isinstance(decoded, dict) else None
    if not isinstance(rows, list):
        raise ValueError("申赎状态响应缺少 datas 列表")
    return rows, hashlib.sha256(payload).hexdigest()


def _status(text: str, opened: set[str], closed: set[str]) -> bool | None:
    if text in opened:
        return True
    if text in closed:
        return False
    return None


def _date_text(value) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _positive_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _next_xshg_submission(current: datetime) -> str:
    import pandas as pd
    from exchange_calendars.exchange_calendar_xshg import XSHGExchangeCalendar

    start = pd.Timestamp(current.date()-timedelta(days=7))
    end = pd.Timestamp(current.date()+timedelta(days=45))
    calendar = XSHGExchangeCalendar(start=start, end=end)
    sessions = calendar.sessions_in_range(pd.Timestamp(current.date()), end)
    eligible = [day.date().isoformat() for day in sessions
                if day.date() > current.date() or current.time() < datetime.strptime("15:00", "%H:%M").time()]
    if not eligible:
        raise ValueError("未找到下一个已核实的国内交易日")
    return eligible[0]


def _is_xshg_session(day: str, current: datetime) -> bool:
    import pandas as pd
    from exchange_calendars.exchange_calendar_xshg import XSHGExchangeCalendar

    calendar = XSHGExchangeCalendar(start=pd.Timestamp(current.date()-timedelta(days=7)),
                                    end=pd.Timestamp(current.date()+timedelta(days=45)))
    return calendar.is_session(pd.Timestamp(day))
