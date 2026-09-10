"""免费实时行情和能力探测实现。"""

from __future__ import annotations

import importlib.util
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from math import isfinite, log, sqrt
from typing import Any

import numpy as np
import pandas as pd

from ..features.breadth import aggregate_returns
from ..features.derivatives import contract_expiry
from ..features.liquidity import combine_proxy_curves, curves_from_amount_rows
from .base import (
    HttpClient,
    ProviderDataError,
    ProviderUnavailable,
    parse_source_time,
    shanghai_now,
)


def fetch_live(provider: str, semantic_type: str, context: dict[str, Any]) -> dict[str, Any]:
    key = (provider, semantic_type)
    handlers = {
        ("tencent", "index"): fetch_tencent_index,
        ("sina", "index"): fetch_sina_index,
        ("eastmoney", "index"): fetch_eastmoney_index,
        ("mootdx", "index"): fetch_mootdx_index,
        ("eastmoney", "breadth"): fetch_eastmoney_breadth,
        ("sina", "breadth"): fetch_sina_breadth,
        ("mootdx", "breadth"): fetch_mootdx_breadth,
        ("tencent", "breadth"): fetch_tencent_breadth,
        ("eastmoney", "limits"): fetch_eastmoney_limits,
        ("xuangubao", "limits"): fetch_xuangubao_limits,
        ("sina_futures", "futures"): fetch_sina_futures,
        ("akshare_futures", "futures"): fetch_akshare_futures,
        ("mootdx", "futures"): fetch_mootdx_futures,
        ("qvix_300_index", "qvix"): fetch_qvix_300_index,
        ("qvix_300_etf", "qvix"): fetch_qvix_300_etf,
        ("sina", "daily_baseline"): fetch_sina_daily_baseline,
        ("baostock", "proxy_curve"): fetch_baostock_proxy_curve,
        ("mootdx", "proxy_curve"): fetch_mootdx_proxy_curve,
        ("sina", "proxy_curve"): fetch_sina_proxy_curve,
        ("tencent", "proxy_curve"): fetch_tencent_proxy_curve,
        ("eastmoney", "proxy_curve"): fetch_eastmoney_proxy_curve,
    }
    if key not in handlers:
        raise ProviderUnavailable(f"未实现的数据源语义: {provider}/{semantic_type}")
    return handlers[key](context)


def _result(
    provider: str,
    semantic_type: str,
    data: dict[str, Any],
    requested_at: datetime,
    source_timestamp: datetime,
    start: float,
    provisional: bool = False,
    quality_flags: list[str] | None = None,
) -> dict[str, Any]:
    received_at = shanghai_now()
    return {
        "provider": provider,
        "semantic_type": semantic_type,
        "data": data,
        "source_timestamp": source_timestamp.isoformat(),
        "requested_at": requested_at.isoformat(),
        "received_at": received_at.isoformat(),
        "provisional": provisional,
        "quality_flags": quality_flags or [],
        "latency_ms": round((time.perf_counter() - start) * 1000.0, 3),
    }


def fetch_tencent_index(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    symbol = str(context.get("symbol", "sh000300"))
    response = HttpClient(context.get("timeout", 20)).get(
        f"https://qt.gtimg.cn/q={symbol}"
    )
    response.encoding = "gbk"
    match = re.search(r'="([^"]+)"', response.text)
    if not match:
        raise ProviderDataError("腾讯实时行情格式错误")
    fields = match.group(1).split("~")
    if len(fields) < 35:
        raise ProviderDataError("腾讯实时行情字段不足")
    timestamp = parse_source_time(fields[30], requested_at)
    data = {
        "symbol": symbol,
        "name": fields[1],
        "last": _number(fields[3], "腾讯现价"),
        "previous_close": _number(fields[4], "腾讯昨收"),
        "open": _number(fields[5], "腾讯开盘"),
        "volume": _optional_number(fields[6]),
        "high": _number(fields[33], "腾讯最高"),
        "low": _number(fields[34], "腾讯最低"),
        "amount": _optional_number(fields[37]) * 1e4 if len(fields) > 37 else None,
    }
    _validate_quote(data)
    return _result("tencent", "index", data, requested_at, timestamp, start)


def fetch_sina_index(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    symbol = str(context.get("symbol", "sh000300"))
    response = HttpClient(context.get("timeout", 20)).get(
        f"https://hq.sinajs.cn/list={symbol}",
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    response.encoding = "gbk"
    match = re.search(r'="([^"]*)"', response.text)
    if not match or not match.group(1):
        raise ProviderDataError("新浪实时行情为空")
    fields = match.group(1).split(",")
    if len(fields) < 32:
        raise ProviderDataError("新浪实时行情字段不足")
    timestamp = parse_source_time(f"{fields[30]} {fields[31]}", requested_at)
    data = {
        "symbol": symbol,
        "name": fields[0],
        "open": _number(fields[1], "新浪开盘"),
        "previous_close": _number(fields[2], "新浪昨收"),
        "last": _number(fields[3], "新浪现价"),
        "high": _number(fields[4], "新浪最高"),
        "low": _number(fields[5], "新浪最低"),
        "volume": _optional_number(fields[8]),
        "amount": _optional_number(fields[9]),
    }
    _validate_quote(data)
    return _result("sina", "index", data, requested_at, timestamp, start)


def fetch_eastmoney_index(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    symbol = str(context.get("symbol", "sh000300"))
    secid = "1." + symbol[2:] if symbol.startswith("sh") else "0." + symbol[2:]
    response = HttpClient(context.get("timeout", 20)).get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params={
            "secid": secid,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f86",
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
    )
    payload = response.json()
    raw = payload.get("data")
    if not isinstance(raw, dict):
        raise ProviderDataError("东方财富指数行情为空")
    scale = 100.0
    timestamp = parse_source_time(datetime.fromtimestamp(int(raw.get("f86", 0))), requested_at)
    data = {
        "symbol": symbol,
        "name": raw.get("f58"),
        "last": _number(raw.get("f43"), "东方财富现价") / scale,
        "high": _number(raw.get("f44"), "东方财富最高") / scale,
        "low": _number(raw.get("f45"), "东方财富最低") / scale,
        "open": _number(raw.get("f46"), "东方财富开盘") / scale,
        "previous_close": _number(raw.get("f60"), "东方财富昨收") / scale,
        "volume": _optional_number(raw.get("f47")),
        "amount": _optional_number(raw.get("f48")),
    }
    _validate_quote(data)
    return _result("eastmoney", "index", data, requested_at, timestamp, start)


def fetch_mootdx_index(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("mootdx") is None:
        raise ProviderUnavailable("未安装mootdx")
    requested_at = shanghai_now()
    start = time.perf_counter()
    from mootdx.quotes import Quotes

    quotes = Quotes.factory(
        market="std",
        bestip=False,
        timeout=min(float(context.get("timeout", 20)), 5.0),
        heartbeat=False,
        auto_retry=False,
        raise_exception=True,
    )
    frame = pd.DataFrame(quotes.client.get_security_quotes([(1, "000300")]))
    if frame is None or frame.empty:
        raise ProviderDataError("mootdx指数行情为空")
    row = frame.iloc[0]
    data = {
        "symbol": "sh000300",
        "name": str(row.get("name", "沪深300")),
        "last": _number(row.get("price", row.get("close")), "mootdx现价"),
        "previous_close": _number(row.get("last_close"), "mootdx昨收"),
        "open": _number(row.get("open"), "mootdx开盘"),
        "high": _number(row.get("high"), "mootdx最高"),
        "low": _number(row.get("low"), "mootdx最低"),
        "volume": _optional_number(row.get("vol")),
        "amount": _optional_number(row.get("amount")),
    }
    _validate_quote(data)
    return _result(
        "mootdx", "index", data, requested_at, requested_at, start,
        quality_flags=["provider_timestamp_unavailable"],
    )


def fetch_eastmoney_breadth(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    response = HttpClient(context.get("timeout", 20)).get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": 1, "pz": 6000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f2,f3,f6,f12,f14,f18",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
    )
    payload = response.json()
    rows = (payload.get("data") or {}).get("diff")
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list) or not rows:
        raise ProviderDataError("东方财富全市场行情为空")
    changes: list[float] = []
    amount = 0.0
    for row in rows:
        value = _optional_number(row.get("f3"))
        price = _optional_number(row.get("f2"))
        if value is None or price is None or price <= 0:
            continue
        changes.append(value / 100.0)
        amount_value = _optional_number(row.get("f6"))
        if amount_value and amount_value > 0:
            amount += amount_value
    if not changes or amount <= 0:
        raise ProviderDataError("东方财富全市场行情字段不完整")
    data = aggregate_returns(changes)
    data["market_amount"] = amount
    data["change_percent_values"] = changes
    return _result(
        "eastmoney",
        "breadth",
        data,
        requested_at,
        requested_at,
        start,
        quality_flags=["provider_timestamp_unavailable"],
    )


def fetch_mootdx_breadth(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("mootdx") is None:
        raise ProviderUnavailable("未安装mootdx")
    from mootdx.quotes import Quotes

    requested_at = shanghai_now()
    started = time.perf_counter()
    client = Quotes.factory(
        market="std",
        bestip=False,
        timeout=min(float(context.get("timeout", 20)), 5.0),
        heartbeat=False,
        auto_retry=False,
        raise_exception=True,
    )
    changes: list[float] = []
    market_amount = 0.0
    try:
        symbols: list[str] = []
        for market in (0, 1):
            stocks = client.stocks(market=market)
            if stocks is None or len(stocks) == 0:
                continue
            frame = pd.DataFrame(stocks)
            code_column = _first_column(frame, ("code", "symbol"))
            for code in frame[code_column].astype(str):
                if _is_a_share_code(code, market):
                    symbols.append(code.zfill(6))
        if not symbols:
            raise ProviderDataError("mootdx未返回A股代码表")
        for offset in range(0, len(symbols), 80):
            frame = client.quotes(symbol=symbols[offset : offset + 80])
            if frame is None or len(frame) == 0:
                continue
            for _, row in pd.DataFrame(frame).iterrows():
                price = _optional_number(row.get("price", row.get("close")))
                previous = _optional_number(row.get("last_close", row.get("pre_close")))
                if price is None or previous is None or price <= 0 or previous <= 0:
                    continue
                changes.append(price / previous - 1.0)
                amount = _optional_number(row.get("amount", row.get("turnover")))
                if amount is not None and amount > 0:
                    market_amount += amount
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    if not changes or market_amount <= 0:
        raise ProviderDataError("mootdx全市场行情字段不完整")
    data = aggregate_returns(changes)
    data["market_amount"] = market_amount
    data["change_percent_values"] = changes
    return _result(
        "mootdx",
        "breadth",
        data,
        requested_at,
        requested_at,
        started,
        quality_flags=["provider_timestamp_unavailable"],
    )


def fetch_sina_breadth(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    started = time.perf_counter()
    client = HttpClient(context.get("timeout", 20))
    count_response = client.get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount",
        params={"node": "hs_a"},
        headers={"Referer": "https://vip.stock.finance.sina.com.cn/"},
    )
    try:
        total = int(str(count_response.json()).strip('"'))
    except (TypeError, ValueError) as error:
        raise ProviderDataError("新浪全市场股票数量无法解析") from error
    pages = max(1, (total + 99) // 100)
    worker_context = dict(context)
    batches: list[list[dict[str, Any]]] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = {
            executor.submit(_fetch_sina_breadth_page, worker_context, page): page
            for page in range(1, pages + 1)
        }
        for future in as_completed(futures):
            try:
                batches.append(future.result())
            except ProviderError:
                failures += 1
    page_coverage = (pages - failures) / pages
    if page_coverage < 0.95:
        raise ProviderDataError(
            f"新浪全市场分页覆盖不足95%: {page_coverage:.1%}"
        )
    changes: list[float] = []
    market_amount = 0.0
    for rows in batches:
        for row in rows:
            symbol = str(row.get("symbol") or "").lower()
            if not symbol.startswith(("sh", "sz")):
                continue
            price = _optional_number(row.get("trade"))
            change = _optional_number(row.get("changepercent"))
            if price is None or price <= 0 or change is None:
                continue
            changes.append(change / 100.0)
            amount = _optional_number(row.get("amount"))
            if amount is not None and amount > 0:
                market_amount += amount
    if not changes or market_amount <= 0:
        raise ProviderDataError("新浪全市场行情为空或字段无效")
    data = aggregate_returns(changes)
    data["market_amount"] = market_amount
    data["change_percent_values"] = changes
    flags = ["provider_timestamp_unavailable"]
    if failures:
        flags.append(f"partial_page_coverage:{page_coverage:.3f}")
    return _result(
        "sina",
        "breadth",
        data,
        requested_at,
        requested_at,
        started,
        provisional=bool(failures),
        quality_flags=flags,
    )


def _fetch_sina_breadth_page(context: dict[str, Any], page: int) -> list[dict[str, Any]]:
    response = HttpClient(min(float(context.get("timeout", 20)), 3.0)).get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
        params={
            "page": page,
            "num": 100,
            "sort": "symbol",
            "asc": 1,
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "page",
        },
        headers={"Referer": "https://vip.stock.finance.sina.com.cn/"},
    )
    rows = response.json()
    if not isinstance(rows, list):
        raise ProviderDataError(f"新浪全市场第{page}页结构无效")
    return rows


def fetch_tencent_breadth(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    started = time.perf_counter()
    client = HttpClient(context.get("timeout", 20))
    universe_response = client.get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": 1,
            "pz": 6000,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f13",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        },
        headers={"Referer": "https://quote.eastmoney.com/"},
    )
    universe = (universe_response.json().get("data") or {}).get("diff") or []
    if isinstance(universe, dict):
        universe = list(universe.values())
    symbols = []
    for row in universe:
        code = str(row.get("f12") or "").zfill(6)
        market = int(row.get("f13", -1))
        if _is_a_share_code(code, market):
            symbols.append(("sh" if market == 1 else "sz") + code)
    if not symbols:
        raise ProviderDataError("腾讯宽度备选源缺少A股代码表")
    changes: list[float] = []
    market_amount = 0.0
    for offset in range(0, len(symbols), 160):
        response = client.get(
            "https://qt.gtimg.cn/q=" + ",".join(symbols[offset : offset + 160]),
            headers={"Referer": "https://gu.qq.com/"},
        )
        for line in response.text.splitlines():
            match = re.search(r'="(.*)"', line)
            if not match:
                continue
            fields = match.group(1).split("~")
            if len(fields) < 7:
                continue
            price = _optional_number(fields[3])
            previous = _optional_number(fields[4])
            volume_hands = _optional_number(fields[6])
            if price is None or previous is None or price <= 0 or previous <= 0:
                continue
            changes.append(price / previous - 1.0)
            if volume_hands is not None and volume_hands > 0:
                market_amount += volume_hands * 100.0 * price
    if not changes or market_amount <= 0:
        raise ProviderDataError("腾讯全市场行情为空或字段无效")
    data = aggregate_returns(changes)
    data["market_amount"] = market_amount
    data["change_percent_values"] = changes
    return _result(
        "tencent",
        "breadth",
        data,
        requested_at,
        requested_at,
        started,
        provisional=True,
        quality_flags=[
            "symbol_universe:eastmoney",
            "market_amount_estimated_from_volume_price",
            "provider_timestamp_unavailable",
        ],
    )


def fetch_eastmoney_limits(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("akshare") is None:
        raise ProviderUnavailable("未安装AKShare")
    requested_at = shanghai_now()
    start = time.perf_counter()
    import akshare as ak

    trade_date = str(context.get("trade_date") or requested_at.date().isoformat())
    compact = trade_date.replace("-", "")
    up_frame = ak.stock_zt_pool_em(date=compact)
    down_frame = ak.stock_zt_pool_dtgc_em(date=compact)
    up_count = 0 if up_frame is None else len(up_frame)
    down_count = 0 if down_frame is None else len(down_frame)
    if up_count == 0 and down_count == 0:
        raise ProviderDataError("东方财富涨跌停池同时为空")
    return _result(
        "eastmoney",
        "limits",
        {"limit_up": up_count, "limit_down": down_count},
        requested_at,
        requested_at,
        start,
        quality_flags=["provider_timestamp_unavailable"],
    )


def fetch_xuangubao_limits(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    client = HttpClient(context.get("timeout", 20))
    up_count = 0
    down_count = 0
    for direction in ("uplimit", "downlimit"):
        response = client.get(
            "https://flash-api.xuangubao.cn/api/surge_stock/stocks",
            params={"normal": "true", direction: "true"},
        )
        data = response.json().get("data") or {}
        fields = data.get("fields") or []
        rows = data.get("items") or []
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise ProviderDataError("选股宝涨跌停字段结构无效")
        for values in rows:
            if not isinstance(values, list):
                continue
            row = dict(zip(fields, values))
            change = _optional_number(row.get("px_change_rate"))
            if direction == "uplimit" and (
                bool(row.get("up_limit")) or (change is not None and change >= 0.095)
            ):
                up_count += 1
            if direction == "downlimit" and change is not None and change <= -0.095:
                down_count += 1
    if up_count == 0 and down_count == 0:
        raise ProviderDataError("选股宝响应无法识别涨跌停字段")
    return _result(
        "xuangubao",
        "limits",
        {"limit_up": up_count, "limit_down": down_count},
        requested_at,
        requested_at,
        start,
        provisional=True,
        quality_flags=["provider_timestamp_unavailable", "limit_semantics_inferred"],
    )


def fetch_sina_futures(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    trade_date = date.fromisoformat(str(context.get("trade_date") or requested_at.date()))
    symbols = _if_contract_symbols(trade_date, 8)
    query = ",".join(f"CFF_RE_{symbol}" for symbol in symbols)
    response = HttpClient(context.get("timeout", 20)).get(
        f"https://hq.sinajs.cn/list={query}",
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    response.encoding = "gbk"
    contracts: list[dict[str, Any]] = []
    source_times: list[datetime] = []
    for line, symbol in zip(response.text.splitlines(), symbols):
        match = re.search(r'="([^"]*)"', line)
        if not match or not match.group(1):
            continue
        fields = match.group(1).split(",")
        numbers = [_optional_number(item) for item in fields]
        positive = [value for value in numbers if value is not None and value > 100]
        if len(positive) < 3:
            continue
        last = positive[0]
        bid = positive[1] if len(positive) > 1 else None
        ask = positive[2] if len(positive) > 2 else None
        timestamps = [item for item in fields if re.fullmatch(r"\d{2}:\d{2}:\d{2}", item)]
        dates = [item for item in fields if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item)]
        if timestamps and dates:
            source_times.append(parse_source_time(f"{dates[-1]} {timestamps[-1]}", requested_at))
        contracts.append(
            {
                "symbol": symbol,
                "last": last,
                "bid": bid,
                "ask": ask,
                "expiry": contract_expiry(symbol).isoformat(),
            }
        )
    if not contracts:
        raise ProviderDataError("新浪明确IF合约行情为空或字段异常")
    source_timestamp = max(source_times) if source_times else requested_at
    flags = [] if source_times else ["provider_timestamp_unavailable"]
    return _result(
        "sina_futures", "futures", {"contracts": contracts},
        requested_at, source_timestamp, start, quality_flags=flags,
    )


def fetch_akshare_futures(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("akshare") is None:
        raise ProviderUnavailable("未安装AKShare")
    requested_at = shanghai_now()
    start = time.perf_counter()
    import akshare as ak

    trade_date = date.fromisoformat(str(context.get("trade_date") or requested_at.date()))
    contracts: list[dict[str, Any]] = []
    errors: list[str] = []
    for symbol in _if_contract_symbols(trade_date, 6):
        try:
            frame = ak.futures_zh_spot(symbol=symbol, market="FF", adjust="0")
            if frame is None or frame.empty:
                continue
            row = frame.iloc[0]
            contracts.append(
                {
                    "symbol": symbol,
                    "last": _number(row.get("current_price", row.get("最新价")), "AKShare期货现价"),
                    "bid": _optional_number(row.get("bid_price", row.get("买价"))),
                    "ask": _optional_number(row.get("ask_price", row.get("卖价"))),
                    "expiry": contract_expiry(symbol).isoformat(),
                }
            )
        except Exception as error:
            errors.append(str(error))
    if not contracts:
        raise ProviderDataError("AKShare明确IF合约行情不可用: " + "; ".join(errors[:2]))
    return _result(
        "akshare_futures", "futures", {"contracts": contracts},
        requested_at, requested_at, start, provisional=True,
        quality_flags=["provider_timestamp_unavailable"],
    )


def fetch_mootdx_futures(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("mootdx") is None:
        raise ProviderUnavailable("未安装mootdx")
    requested_at = shanghai_now()
    start = time.perf_counter()
    from mootdx.quotes import Quotes

    trade_date = date.fromisoformat(str(context.get("trade_date") or requested_at.date()))
    symbols = _if_contract_symbols(trade_date, 6)
    quotes = Quotes.factory(market="ext", multithread=True, heartbeat=True)
    frame = quotes.quotes(symbol=symbols)
    if frame is None or frame.empty:
        raise ProviderDataError("mootdx扩展市场IF行情为空")
    contracts = []
    for _, row in frame.iterrows():
        symbol = str(row.get("code", row.get("symbol", ""))).upper()
        if not symbol.startswith("IF"):
            continue
        contracts.append(
            {
                "symbol": symbol,
                "last": _number(row.get("price", row.get("close")), "mootdx期货现价"),
                "bid": _optional_number(row.get("bid1")),
                "ask": _optional_number(row.get("ask1")),
                "expiry": contract_expiry(symbol).isoformat(),
            }
        )
    if not contracts:
        raise ProviderDataError("mootdx未返回明确IF合约")
    return _result(
        "mootdx", "futures", {"contracts": contracts},
        requested_at, requested_at, start,
        quality_flags=["provider_timestamp_unavailable"],
    )


def fetch_qvix_300_index(context: dict[str, Any]) -> dict[str, Any]:
    return _fetch_qvix_akshare(
        context,
        "qvix_300_index",
        "300股指QVIX",
        "index_option_300index_min_qvix",
        "index_option_300index_qvix",
    )


def fetch_qvix_300_etf(context: dict[str, Any]) -> dict[str, Any]:
    return _fetch_qvix_akshare(
        context,
        "qvix_300_etf",
        "300ETF QVIX",
        "index_option_300etf_min_qvix",
        "index_option_300etf_qvix",
    )


def _fetch_qvix_akshare(
    context: dict[str, Any],
    provider: str,
    symbol: str,
    minute_function: str,
    daily_function: str,
) -> dict[str, Any]:
    if importlib.util.find_spec("akshare") is None:
        raise ProviderUnavailable("未安装AKShare")
    import akshare as ak

    requested_at = shanghai_now()
    start = time.perf_counter()
    minute = pd.DataFrame(getattr(ak, minute_function)())
    if minute.empty or not {"time", "qvix"}.issubset(minute.columns):
        raise ProviderDataError(f"{symbol}分时数据为空")
    minute["qvix"] = pd.to_numeric(minute["qvix"], errors="coerce")
    minute = minute.dropna(subset=["qvix"])
    if minute.empty:
        raise ProviderDataError(f"{symbol}当日分时值全部为空")
    latest = minute.iloc[-1]
    trade_date = date.fromisoformat(str(context["trade_date"]))
    timestamp = parse_source_time(
        f"{trade_date.isoformat()} {latest['time']}", requested_at
    )
    daily = pd.DataFrame(getattr(ak, daily_function)())
    minute_times = pd.to_datetime(
        trade_date.isoformat() + " " + minute["time"].astype(str),
        errors="coerce",
    )
    latest_time = minute_times.iloc[-1]
    previous_5m = None
    if not pd.isna(latest_time):
        cutoff = latest_time - pd.Timedelta(minutes=5)
        candidates = minute.loc[minute_times <= cutoff, "qvix"]
        if not candidates.empty:
            previous_5m = float(candidates.iloc[-1])
    previous_close = None
    earliest = None
    latest_daily = None
    if not daily.empty and daily.shape[1] >= 2:
        date_column = next(
            (name for name in daily.columns if "date" in str(name).lower() or "日期" in str(name)),
            daily.columns[0],
        )
        value_columns = [name for name in daily.columns if name != date_column]
        value_column = next(
            (name for name in value_columns if "close" in str(name).lower() or "qvix" in str(name).lower()),
            value_columns[-1],
        )
        daily[date_column] = pd.to_datetime(daily[date_column], errors="coerce")
        daily[value_column] = pd.to_numeric(daily[value_column], errors="coerce")
        daily = daily.dropna(subset=[date_column, value_column]).sort_values(date_column)
        past = daily[daily[date_column].dt.date < trade_date]
        if not past.empty:
            previous_close = float(past.iloc[-1][value_column])
        if not daily.empty:
            earliest = daily.iloc[0][date_column].isoformat()
            latest_daily = daily.iloc[-1][date_column].isoformat()
    data = {
        "symbol": symbol,
        "value": float(latest["qvix"]),
        "previous_close": previous_close,
        "previous_5m": previous_5m,
        "earliest_timestamp": earliest,
        "latest_timestamp": timestamp.isoformat(),
        "latest_daily_timestamp": latest_daily,
        "returned_rows": len(minute),
    }
    return _result(provider, "qvix", data, requested_at, timestamp, start)


def fetch_sina_daily_baseline(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    start = time.perf_counter()
    symbol = str(context.get("symbol", "sh000300"))
    response = HttpClient(context.get("timeout", 20)).get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": symbol, "scale": 240, "ma": 5, "datalen": 80},
    )
    rows = response.json()
    if not isinstance(rows, list) or len(rows) < 21:
        raise ProviderDataError("新浪日线不足21条，无法计算前一日波动率")
    frame = pd.DataFrame(rows)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["day"] = pd.to_datetime(frame["day"], errors="coerce")
    frame = frame.dropna(subset=["close", "day"]).sort_values("day")
    returns = np.log(frame["close"] / frame["close"].shift(1))
    annualized = returns.rolling(20).std(ddof=1) * sqrt(252)
    daily_sigma = float(annualized.iloc[-2] / sqrt(252))
    if not isfinite(daily_sigma) or daily_sigma <= 0:
        raise ProviderDataError("新浪日线波动率无效")
    source_timestamp = parse_source_time(
        frame.iloc[-1]["day"].date().isoformat(), requested_at
    )
    return _result(
        "sina",
        "daily_baseline",
        {
            "daily_sigma": daily_sigma,
            "median_daily_market_amount_20": None,
            "earliest_timestamp": frame.iloc[0]["day"].isoformat(),
            "latest_timestamp": frame.iloc[-1]["day"].isoformat(),
            "returned_rows": len(frame),
        },
        requested_at,
        source_timestamp,
        start,
    )


def fetch_baostock_proxy_curve(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("baostock") is None:
        raise ProviderUnavailable("未安装BaoStock")
    import baostock as bs

    requested_at = shanghai_now()
    started = time.perf_counter()
    end = date.fromisoformat(str(context["trade_date"]))
    history_days = int(context.get("proxy_history_natural_days", 180))
    start = end - timedelta(days=history_days)
    symbols = _proxy_symbols(context)
    login = bs.login()
    if login.error_code != "0":
        raise ProviderUnavailable(f"BaoStock登录失败: {login.error_msg}")
    rows: list[dict[str, Any]] = []
    try:
        for symbol in symbols:
            code = f"{symbol[:2]}.{symbol[2:]}"
            result = bs.query_history_k_data_plus(
                code,
                "date,time,code,open,high,low,close,volume,amount",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="5",
                adjustflag="3",
            )
            if result.error_code != "0":
                continue
            while result.next():
                values = result.get_row_data()
                timestamp = _parse_baostock_timestamp(values[0], values[1])
                bucket = _proxy_bucket(timestamp)
                if bucket is None:
                    continue
                amount = _optional_number(values[8])
                if amount is None:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "trade_date": timestamp.date().isoformat(),
                        "bucket_5m": bucket,
                        "amount": amount,
                    }
                )
    finally:
        bs.logout()
    return _proxy_curve_result("baostock", rows, requested_at, started)


def fetch_mootdx_proxy_curve(context: dict[str, Any]) -> dict[str, Any]:
    if importlib.util.find_spec("mootdx") is None:
        raise ProviderUnavailable("未安装mootdx")
    from mootdx.quotes import Quotes

    requested_at = shanghai_now()
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    client = Quotes.factory(
        market="std",
        bestip=False,
        timeout=min(float(context.get("timeout", 20)), 5.0),
        heartbeat=False,
        auto_retry=False,
        raise_exception=True,
    )
    try:
        for symbol in _proxy_symbols(context):
            frame = client.bars(symbol=symbol[2:], frequency=0, offset=0, count=800)
            if frame is None or len(frame) == 0:
                continue
            frame = pd.DataFrame(frame).reset_index()
            timestamp_column = _first_column(frame, ("datetime", "date", "time"))
            amount_column = _first_column(frame, ("amount", "turnover"))
            rows.extend(
                _proxy_rows_from_frame(
                    frame,
                    symbol,
                    timestamp_column,
                    amount_column,
                )
            )
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    return _proxy_curve_result("mootdx", rows, requested_at, started)


def fetch_sina_proxy_curve(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    started = time.perf_counter()
    client = HttpClient(context.get("timeout", 20))
    rows: list[dict[str, Any]] = []
    for symbol in _proxy_symbols(context):
        response = client.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": symbol, "scale": 5, "ma": "no", "datalen": 1023},
        )
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            continue
        frame = pd.DataFrame(payload)
        if not {"day", "volume", "close"}.issubset(frame.columns):
            continue
        frame["估算成交额"] = pd.to_numeric(frame["volume"], errors="coerce") * pd.to_numeric(
            frame["close"], errors="coerce"
        )
        rows.extend(_proxy_rows_from_frame(frame, symbol, "day", "估算成交额"))
    return _proxy_curve_result(
        "sina",
        rows,
        requested_at,
        started,
        quality_flags=["amount_estimated_from_volume_price"],
    )


def fetch_tencent_proxy_curve(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    started = time.perf_counter()
    client = HttpClient(context.get("timeout", 20))
    rows: list[dict[str, Any]] = []
    for symbol in _proxy_symbols(context):
        response = client.get(
            "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
            params={"param": f"{symbol},m5,,320"},
        )
        payload = response.json().get("data", {}).get(symbol, {})
        values = payload.get("m5") or payload.get("m5_data") or []
        for item in values:
            if not isinstance(item, list) or len(item) < 6:
                continue
            timestamp = pd.to_datetime(str(item[0]), errors="coerce")
            if pd.isna(timestamp):
                continue
            bucket = _proxy_bucket(timestamp.to_pydatetime())
            if bucket is None:
                continue
            amount = _optional_number(item[6] if len(item) > 6 else None)
            if amount is None:
                volume = _optional_number(item[5])
                close = _optional_number(item[2])
                if volume is None or close is None:
                    continue
                amount = volume * close
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": timestamp.date().isoformat(),
                    "bucket_5m": bucket,
                    "amount": amount,
                }
            )
    return _proxy_curve_result("tencent", rows, requested_at, started)


def fetch_eastmoney_proxy_curve(context: dict[str, Any]) -> dict[str, Any]:
    requested_at = shanghai_now()
    started = time.perf_counter()
    client = HttpClient(context.get("timeout", 20))
    rows: list[dict[str, Any]] = []
    for symbol in _proxy_symbols(context):
        secid = f"1.{symbol[2:]}" if symbol.startswith("sh") else f"0.{symbol[2:]}"
        response = client.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "klt": 5,
                "fqt": 0,
                "lmt": 1000,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
        )
        values = (response.json().get("data") or {}).get("klines") or []
        for line in values:
            fields = str(line).split(",")
            if len(fields) < 11:
                continue
            timestamp = pd.to_datetime(fields[0], errors="coerce")
            amount = _optional_number(fields[6])
            if pd.isna(timestamp) or amount is None:
                continue
            bucket = _proxy_bucket(timestamp.to_pydatetime())
            if bucket is None:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": timestamp.date().isoformat(),
                    "bucket_5m": bucket,
                    "amount": amount,
                }
            )
    return _proxy_curve_result("eastmoney", rows, requested_at, started)


def _proxy_curve_result(
    provider: str,
    rows: list[dict[str, Any]],
    requested_at: datetime,
    started: float,
    quality_flags: list[str] | None = None,
) -> dict[str, Any]:
    curves = curves_from_amount_rows(rows)
    if not curves:
        raise ProviderDataError(f"{provider}未返回完整ETF五分钟成交额曲线")
    composite = combine_proxy_curves([item["curve"] for item in curves])
    if not composite:
        raise ProviderDataError(f"{provider}代理成交曲线为空")
    dates = sorted({str(item["trade_date"]) for item in rows})
    data = {
        "curve": composite,
        "symbols": [item["symbol"] for item in curves],
        "symbol_curves": curves,
        "sample_days": min(item["sample_days"] for item in curves),
        "returned_rows": len(rows),
        "earliest_timestamp": dates[0] if dates else None,
        "latest_timestamp": dates[-1] if dates else None,
    }
    source_timestamp = parse_source_time(dates[-1], requested_at)
    return _result(
        provider,
        "proxy_curve",
        data,
        requested_at,
        source_timestamp,
        started,
        provisional=bool(quality_flags),
        quality_flags=quality_flags,
    )


def _proxy_rows_from_frame(
    frame: pd.DataFrame,
    symbol: str,
    timestamp_column: str,
    amount_column: str,
) -> list[dict[str, Any]]:
    timestamps = pd.to_datetime(frame[timestamp_column], errors="coerce")
    amounts = pd.to_numeric(frame[amount_column], errors="coerce")
    rows: list[dict[str, Any]] = []
    for timestamp, amount in zip(timestamps, amounts):
        if pd.isna(timestamp) or pd.isna(amount):
            continue
        bucket = _proxy_bucket(timestamp.to_pydatetime())
        if bucket is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "trade_date": timestamp.date().isoformat(),
                "bucket_5m": bucket,
                "amount": float(amount),
            }
        )
    return rows


def _proxy_bucket(timestamp: datetime) -> int | None:
    minutes = timestamp.hour * 60 + timestamp.minute
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return min(24, (minutes - (9 * 60 + 30)) // 5)
    if 13 * 60 <= minutes <= 15 * 60:
        return min(48, 24 + (minutes - 13 * 60) // 5)
    return None


def _parse_baostock_timestamp(day: str, value: str) -> datetime:
    digits = re.sub(r"\D", "", str(value))
    if len(digits) >= 14:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
    return datetime.fromisoformat(f"{day} 15:00:00")


def _proxy_symbols(context: dict[str, Any]) -> list[str]:
    values = context.get("proxy_symbols") or ["sh510300", "sz159919", "sh510050", "sh510500"]
    return [str(value).lower() for value in values]


def _is_a_share_code(code: str, market: int) -> bool:
    value = str(code).zfill(6)
    if market == 1:
        return value.startswith(("600", "601", "603", "605", "688"))
    if market == 0:
        return value.startswith(("000", "001", "002", "003", "300", "301"))
    return False


def _first_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    for name in candidates:
        if name in frame.columns:
            return name
    raise ProviderDataError("分钟数据缺少时间或成交额字段")


def _if_contract_symbols(start_date: date, count: int) -> list[str]:
    values = []
    year = start_date.year
    month = start_date.month
    for offset in range(count):
        total = (year * 12 + month - 1) + offset
        contract_year, contract_month_zero = divmod(total, 12)
        values.append(f"IF{contract_year % 100:02d}{contract_month_zero + 1:02d}")
    return values


def _number(value: Any, name: str) -> float:
    number = _optional_number(value)
    if number is None:
        raise ProviderDataError(f"{name}为空或无效")
    return number


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _validate_quote(data: dict[str, Any]) -> None:
    for key in ("last", "previous_close", "open", "high", "low"):
        if float(data[key]) <= 0:
            raise ProviderDataError(f"指数行情字段必须为正数: {key}")
    if data["high"] < data["low"]:
        raise ProviderDataError("指数最高价低于最低价")
    if not data["low"] <= data["last"] <= data["high"]:
        raise ProviderDataError("指数现价不在最高最低范围内")
