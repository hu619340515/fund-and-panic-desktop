"""东方财富日线历史与沪深 A 股成交额基线。"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

from .base import HttpClient, ProviderDataError, ProviderError, ProviderUnavailable


_EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_MARKET_AMOUNT_SERIES = {
    "1.000002": {"code": "000002", "name": "Ａ股指数", "market": "上海A股"},
    "0.399107": {"code": "399107", "name": "深证Ａ指", "market": "深圳A股"},
}
_CACHE_VERSION = 1


def fetch_index_history(
    symbol: str,
    start: date,
    end: date,
    timeout_seconds: float = 20.0,
) -> list[dict[str, Any]]:
    """返回指定指数区间内的真实日线，日期边界均包含。"""
    if start > end:
        raise ValueError("指数历史开始日期不能晚于结束日期")
    secid = _symbol_to_secid(symbol)
    series, _ = _ensure_series(secid, start, end, timeout_seconds)
    return [
        {
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "amount": row["amount"],
        }
        for row in series["rows"]
        if start <= date.fromisoformat(row["date"]) <= end
    ]


def fetch_market_amount_history(
    as_of: date,
    natural_days: int = 450,
    timeout_seconds: float = 20.0,
) -> list[dict[str, Any]]:
    """返回 as_of 之前沪深 A 股每日合计成交额，单位为人民币元。"""
    return fetch_market_amount_history_result(
        as_of,
        natural_days=natural_days,
        timeout_seconds=timeout_seconds,
    )["rows"]


def fetch_market_amount_history_result(
    as_of: date,
    natural_days: int = 450,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """返回成交额序列及来源、覆盖范围、单位与缓存状态。"""
    if natural_days < 30:
        raise ValueError("成交额历史至少需要 30 个自然日")
    start = as_of - timedelta(days=natural_days)
    end = as_of - timedelta(days=1)
    sources: list[dict[str, Any]] = []
    values_by_source: list[dict[str, float]] = []
    errors: list[str] = []
    for secid, expected in _MARKET_AMOUNT_SERIES.items():
        try:
            series, cache_hit = _ensure_series(
                secid,
                start,
                end,
                timeout_seconds,
                expected=expected,
            )
            amounts = {
                row["date"]: float(row["amount"])
                for row in series["rows"]
                if start <= date.fromisoformat(row["date"]) <= end
                and float(row["amount"]) > 0
            }
            if not amounts:
                raise ProviderDataError(f"{expected['market']}历史成交额为空")
            recent_amounts = list(amounts.values())[-20:]
            if max(recent_amounts) < 100_000_000:
                raise ProviderDataError(
                    f"{expected['market']}成交额数量级异常，无法确认为人民币元"
                )
            values_by_source.append(amounts)
            sources.append(
                {
                    "secid": secid,
                    "code": expected["code"],
                    "name": series["name"],
                    "market": expected["market"],
                    "rows": len(amounts),
                    "earliest_date": min(amounts),
                    "latest_date": max(amounts),
                    "cache_hit": cache_hit,
                }
            )
        except (ProviderError, OSError, ValueError) as error:
            errors.append(f"{expected['market']}: {error}")
    if errors or len(values_by_source) != 2:
        return {
            "available": False,
            "rows": [],
            "amount_unit": "CNY",
            "sources": sources,
            "error": "; ".join(errors) or "沪深 A 股成交额来源不完整",
        }
    common_dates = sorted(set(values_by_source[0]) & set(values_by_source[1]))
    rows = [
        {
            "date": day,
            "amount": values_by_source[0][day] + values_by_source[1][day],
        }
        for day in common_dates
        if date.fromisoformat(day) < as_of
    ]
    return {
        "available": bool(rows),
        "rows": rows,
        "amount_unit": "CNY",
        "sources": sources,
        "earliest_date": rows[0]["date"] if rows else None,
        "latest_date": rows[-1]["date"] if rows else None,
        "common_trading_days": len(rows),
        "cache_hit": all(source["cache_hit"] for source in sources),
        "error": None if rows else "沪深 A 股历史没有共同交易日",
    }


def _ensure_series(
    secid: str,
    start: date,
    end: date,
    timeout_seconds: float,
    expected: dict[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    cache_path = _cache_path(secid)
    cached = _read_cache(cache_path, secid)
    if cached.get("rows"):
        try:
            _validate_identity(cached, secid, expected)
        except ProviderDataError:
            cached = {"version": _CACHE_VERSION, "secid": secid, "rows": []}
    ranges: list[tuple[date, date]] = []
    covered_from = _optional_date(cached.get("covered_from"))
    covered_through = _optional_date(cached.get("covered_through"))
    if covered_from is None or covered_through is None:
        ranges.append((start, end))
    else:
        if start < covered_from:
            ranges.append((start, min(end, covered_from - timedelta(days=1))))
        if end > covered_through:
            # 每次只重叠下载最近两周，既避免逐次全抓，也确保周末和长假尾段
            # 不会形成东方财富无 K 线的纯空请求。
            ranges.append((max(start, covered_through - timedelta(days=14)), end))
    ranges = [(first, last) for first, last in ranges if first <= last]
    if not ranges:
        return cached, True

    merged = {row["date"]: row for row in cached.get("rows", [])}
    name = str(cached.get("name") or "")
    code = str(cached.get("code") or "")
    market = cached.get("market")
    for first, last in ranges:
        downloaded = _download_series(secid, first, last, timeout_seconds)
        _validate_identity(downloaded, secid, expected)
        name = downloaded["name"]
        code = downloaded["code"]
        market = downloaded["market"]
        merged.update({row["date"]: row for row in downloaded["rows"]})
    result = {
        "version": _CACHE_VERSION,
        "secid": secid,
        "code": code,
        "name": name,
        "market": market,
        "covered_from": min(start, covered_from).isoformat() if covered_from else start.isoformat(),
        "covered_through": max(end, covered_through).isoformat() if covered_through else end.isoformat(),
        "updated_at": datetime.now().astimezone().isoformat(),
        "rows": [merged[key] for key in sorted(merged)],
    }
    _write_cache(cache_path, result)
    return result, False


def _download_series(
    secid: str,
    start: date,
    end: date,
    timeout_seconds: float,
) -> dict[str, Any]:
    response = HttpClient(timeout_seconds).get(
        _EASTMONEY_KLINE_URL,
        params={
            "secid": secid,
            "klt": 101,
            "fqt": 0,
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "lmt": max(60, (end - start).days + 10),
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
    )
    try:
        payload = response.json()
    except Exception as error:
        raise ProviderDataError(f"东方财富 {secid} 历史响应不是有效 JSON") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or payload.get("rc") != 0:
        raise ProviderDataError(f"东方财富 {secid} 历史响应为空")
    rows = []
    for raw in data.get("klines") or []:
        fields = str(raw).split(",")
        if len(fields) < 7:
            continue
        try:
            day = date.fromisoformat(fields[0])
            numbers = [float(value) for value in fields[1:7]]
        except (TypeError, ValueError):
            continue
        if not all(isfinite(value) for value in numbers):
            continue
        if not start <= day <= end:
            continue
        open_value, close, high, low, volume, amount = numbers
        if min(open_value, close, high, low) <= 0 or volume < 0 or amount < 0:
            continue
        rows.append(
            {
                "date": day.isoformat(),
                "open": open_value,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        )
    return {
        "secid": secid,
        "code": str(data.get("code") or ""),
        "name": str(data.get("name") or ""),
        "market": data.get("market"),
        "rows": rows,
    }


def _validate_identity(
    series: dict[str, Any],
    secid: str,
    expected: dict[str, str] | None,
) -> None:
    expected_market = int(secid.split(".", 1)[0])
    expected_code = secid.split(".", 1)[1]
    if (
        series.get("code") != expected_code
        or series.get("market") != expected_market
        or not series.get("name")
    ):
        raise ProviderDataError(
            f"东方财富 {secid} 标识不符："
            f"{series.get('market')}.{series.get('code')} {series.get('name')}"
        )
    if not series.get("rows"):
        raise ProviderDataError(f"东方财富 {secid} 请求区间没有有效日线")
    if not series["code"] or not series["name"]:
        raise ProviderDataError(f"东方财富 {secid} 历史标识缺失")
    if expected and (
        series["code"] != expected["code"] or series["name"] != expected["name"]
    ):
        raise ProviderDataError(
            f"东方财富 {secid} 标识不符：{series['code']} {series['name']}"
        )


def _symbol_to_secid(symbol: str) -> str:
    value = str(symbol).strip().lower()
    if "." in value and value.split(".", 1)[0] in {"0", "1"}:
        return value
    if value.startswith("sh") and len(value) == 8:
        return f"1.{value[2:]}"
    if value.startswith("sz") and len(value) == 8:
        return f"0.{value[2:]}"
    raise ValueError(f"不支持的指数代码: {symbol}")


def _cache_path(secid: str) -> Path:
    directory = os.environ.get("PANIC_INDEX_CACHE_DIRECTORY")
    if not directory:
        raise ProviderUnavailable("未配置 PANIC_INDEX_CACHE_DIRECTORY，历史数据不缓存")
    root = Path(directory).expanduser().resolve() / "history"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"eastmoney-{secid.replace('.', '_')}.json"


def _read_cache(path: Path, secid: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": _CACHE_VERSION, "secid": secid, "rows": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": _CACHE_VERSION, "secid": secid, "rows": []}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _CACHE_VERSION
        or payload.get("secid") != secid
        or not isinstance(payload.get("rows"), list)
    ):
        return {"version": _CACHE_VERSION, "secid": secid, "rows": []}
    return payload


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _optional_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
