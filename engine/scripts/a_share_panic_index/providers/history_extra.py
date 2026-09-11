"""CFFEX 明确 IF 合约与开盘红市场宽度历史数据。"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

import requests

from ..features.derivatives import contract_expiry
from .base import ProviderDataError, ProviderTransportError, ProviderUnavailable

_CFFEX_URL_TEMPLATE = "http://www.cffex.com.cn/sj/historysj/{month}/zip/{month}.zip"
_KAIPANHONG_URL = "https://apphis.kaipanhong.com/w1/api/index.php"
_CACHE_VERSION = 1
_REQUEST_TIMEOUT_SECONDS = 6.0
_TOTAL_DEADLINE_SECONDS = 100.0
_MAX_CFFEX_ARCHIVE_BYTES = 50 * 1024 * 1024
_MAX_CFFEX_ENTRY_BYTES = 5 * 1024 * 1024
_MAX_REPORTED_ERRORS = 80
_IF_CONTRACT_PATTERN = re.compile(r"^IF\d{4}$")
_CFFEX_DAY_PATTERN = re.compile(r"(?<!\d)(20\d{6})(?!\d)")
_KAIPANHONG_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": (
        "Dalvik/2.1.0 (Linux; U; Android 12; "
        "2206123SC Build/c069a49.2)"
    ),
    "Accept-Encoding": "gzip",
}
_KAIPANHONG_BASE_FORM = {
    "a": "HisZhangFuDetail",
    "c": "HisHomeDingPan",
    "PhoneOSNew": "1",
    "DeviceID": "1a609dd6-b2b8-3bf9-ac40-a77581551454",
    "VerSion": "6.0.6",
    "Token": "0",
    "UserID": "0",
    "Red": "1",
    "apiv": "w45",
}


def fetch_extra_history(days: list[str], as_of: date) -> dict[str, Any]:
    """返回 ``as_of`` 之前指定日期的 IF 合约和市场宽度历史。

    网络失败、无交易数据和不合格响应均按日或按月写入 ``errors``，其余
    已成功日期仍会返回。函数不会用邻近日的数据填补缺失日。
    """
    if not isinstance(as_of, date):
        raise TypeError("as_of 必须是 date")
    if not isinstance(days, list):
        raise TypeError("days 必须是 ISO 日期字符串列表")

    normalized_days, errors = _normalize_days(days, as_of)
    result: dict[str, Any] = {
        "futures": {},
        "breadth": {},
        "errors": errors,
        "sources": {
            "futures": {
                "provider": "cffex",
                "name": "中国金融期货交易所",
                "url_template": _CFFEX_URL_TEMPLATE,
                "scope": "明确月份 IF 合约日行情",
                "price_field": "今收盘",
                "expiry_basis": "已完成交割月中对应合约的官方最后出现日",
                "requested_days": len(normalized_days),
                "returned_days": 0,
                "downloaded_months": [],
                "cached_months": [],
            },
            "breadth": {
                "provider": "kaipanhong",
                "name": "开盘红历史涨跌分布",
                "endpoint": _KAIPANHONG_URL,
                "scope": "上游返回的上涨、下跌、平盘及 ZT/DT 总数",
                "scope_warning": (
                    "市场范围和停牌排除口径未经官方文档确认；ZT/DT 为上游总数；"
                    "不依据涨跌幅分桶推导 5%/7% 阈值或收益中位数。"
                ),
                "requested_days": len(normalized_days),
                "returned_days": 0,
                "downloaded_days": 0,
                "cached_days": 0,
            },
        },
    }
    if not normalized_days:
        result["errors"] = _compact_errors(result["errors"])
        return result

    try:
        cache_root = _cache_root()
    except ProviderUnavailable as error:
        result["errors"].append(str(error))
        result["errors"] = _compact_errors(result["errors"])
        return result

    deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS
    requested_by_month: dict[str, list[str]] = {}
    for day in normalized_days:
        requested_by_month.setdefault(day[:7].replace("-", ""), []).append(day)

    current_month = as_of.strftime("%Y%m")
    loaded_months: dict[str, dict[str, list[dict[str, Any]]]] = {}
    complete_months: set[str] = set()
    for month, month_days in sorted(requested_by_month.items()):
        cached_record = _read_cffex_cache(cache_root / f"cffex-{month}.json", month)
        cached = cached_record[0] if cached_record is not None else None
        cache_complete = cached_record[1] if cached_record is not None else False
        month_rows: dict[str, list[dict[str, Any]]] | None = None
        if cached is not None and month < current_month and cache_complete:
            month_rows = cached
            result["sources"]["futures"]["cached_months"].append(month)
        elif time.monotonic() >= deadline:
            result["errors"].append(f"CFFEX {month}：超过总截止时间")
            month_rows = cached
        else:
            try:
                timeout = min(_REQUEST_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
                content = _download_cffex_month(month, timeout)
                month_rows = _parse_cffex_zip(content, month)
                _write_json_atomic(
                    cache_root / f"cffex-{month}.json",
                    {
                        "version": _CACHE_VERSION,
                        "kind": "cffex_month",
                        "month": month,
                        "complete": month < current_month,
                        "fetched_at": datetime.now().astimezone().isoformat(),
                        "days": month_rows,
                    },
                )
                result["sources"]["futures"]["downloaded_months"].append(month)
            except (OSError, ProviderDataError, ProviderTransportError, ValueError) as error:
                if cached is not None:
                    month_rows = cached
                    result["sources"]["futures"]["cached_months"].append(month)
                    result["errors"].append(
                        f"CFFEX {month} 更新失败，使用已有缓存：{error}"
                    )
                else:
                    result["errors"].append(f"CFFEX {month}：{error}")

        if month_rows is not None:
            loaded_months[month] = month_rows
            if (month < current_month and cache_complete) or (
                month < current_month
                and month in result["sources"]["futures"]["downloaded_months"]
            ):
                complete_months.add(month)

        for day in month_days:
            contracts = month_rows.get(day) if month_rows is not None else None
            if contracts:
                result["futures"][day] = contracts
            else:
                result["errors"].append(f"CFFEX {day}：没有有效 IF 日行情")

    _annotate_official_expiries(
        result["futures"], _derive_official_expiries(loaded_months, complete_months)
    )

    missing_breadth: list[str] = []
    for day in normalized_days:
        cache_path = cache_root / f"kaipanhong-{day}.json"
        cached = _read_breadth_cache(cache_path, day)
        if cached is None:
            missing_breadth.append(day)
        else:
            result["breadth"][day] = cached
            result["sources"]["breadth"]["cached_days"] += 1

    _download_missing_breadth(
        missing_breadth,
        cache_root,
        deadline,
        result["breadth"],
        result["errors"],
        result["sources"]["breadth"],
    )
    result["sources"]["futures"]["returned_days"] = len(result["futures"])
    result["sources"]["breadth"]["returned_days"] = len(result["breadth"])
    result["errors"] = _compact_errors(result["errors"])
    return result


def _normalize_days(days: list[str], as_of: date) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in days:
        if not isinstance(raw, str):
            errors.append(f"无效历史日期：{raw!r}")
            continue
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            errors.append(f"无效历史日期：{raw!r}")
            continue
        if raw != parsed.isoformat():
            errors.append(f"历史日期必须使用 ISO 格式：{raw!r}")
            continue
        if parsed >= as_of:
            errors.append(f"历史日期 {raw} 不早于 as_of {as_of.isoformat()}")
            continue
        if raw not in seen:
            normalized.append(raw)
            seen.add(raw)
    normalized.sort()
    return normalized, errors


def _download_cffex_month(month: str, timeout_seconds: float) -> bytes:
    url = _CFFEX_URL_TEMPLATE.format(month=month)
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "http://www.cffex.com.cn/"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise ProviderTransportError(str(error)) from error
    content = bytes(response.content)
    if not content or len(content) > _MAX_CFFEX_ARCHIVE_BYTES:
        raise ProviderDataError(f"CFFEX {month} ZIP 大小异常：{len(content)} 字节")
    return content


def _parse_cffex_zip(content: bytes, expected_month: str) -> dict[str, list[dict[str, Any]]]:
    """在内存中解析 CFFEX 月度 ZIP，返回每日明确 IF 合约收盘。"""
    if not re.fullmatch(r"20\d{4}", expected_month):
        raise ValueError(f"无效 CFFEX 月份：{expected_month}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as error:
        raise ProviderDataError(f"CFFEX {expected_month} 响应不是有效 ZIP") from error

    parsed_days: dict[str, list[dict[str, Any]]] = {}
    try:
        csv_entries = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
        if not csv_entries or len(csv_entries) > 40:
            raise ProviderDataError(
                f"CFFEX {expected_month} ZIP 日文件数量异常：{len(csv_entries)}"
            )
        for entry in csv_entries:
            if entry.file_size <= 0 or entry.file_size > _MAX_CFFEX_ENTRY_BYTES:
                raise ProviderDataError(
                    f"CFFEX {expected_month} 日文件大小异常：{entry.filename}"
                )
            match = _CFFEX_DAY_PATTERN.search(Path(entry.filename).name)
            if match is None:
                continue
            raw_day = match.group(1)
            if raw_day[:6] != expected_month:
                raise ProviderDataError(
                    f"CFFEX {expected_month} ZIP 含错月文件：{entry.filename}"
                )
            try:
                day = date.fromisoformat(
                    f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
                ).isoformat()
            except ValueError as error:
                raise ProviderDataError(
                    f"CFFEX {expected_month} 日文件日期无效：{entry.filename}"
                ) from error
            text = _decode_cffex_csv(archive.read(entry), entry.filename)
            contracts = _parse_cffex_csv(text, entry.filename)
            if contracts:
                parsed_days[day] = contracts
    finally:
        archive.close()
    if not parsed_days:
        raise ProviderDataError(f"CFFEX {expected_month} ZIP 没有有效 IF 日行情")
    return {day: parsed_days[day] for day in sorted(parsed_days)}


def _decode_cffex_csv(content: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ProviderDataError(f"CFFEX 日文件编码无法识别：{filename}")


def _parse_cffex_csv(text: str, filename: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    fields = {str(field).strip() for field in (reader.fieldnames or [])}
    required = {"合约代码", "今收盘"}
    if not required.issubset(fields):
        raise ProviderDataError(f"CFFEX 日文件缺少明确表头：{filename}")
    by_symbol: dict[str, dict[str, Any]] = {}
    for raw_row in reader:
        row = {str(key).strip(): value for key, value in raw_row.items() if key is not None}
        symbol = str(row.get("合约代码") or "").strip().upper()
        if not _IF_CONTRACT_PATTERN.fullmatch(symbol):
            continue
        last = _positive_number(row.get("今收盘"))
        if last is None:
            continue
        contract = {"symbol": symbol, "last": last}
        previous = by_symbol.get(symbol)
        if previous is not None and previous != contract:
            raise ProviderDataError(f"CFFEX 日文件同一合约重复且收盘冲突：{filename} {symbol}")
        by_symbol[symbol] = contract
    return [by_symbol[symbol] for symbol in sorted(by_symbol)]


def _download_missing_breadth(
    days: list[str],
    cache_root: Path,
    deadline: float,
    output: dict[str, dict[str, int]],
    errors: list[str],
    source: dict[str, Any],
) -> None:
    if not days:
        return
    pending_days = list(days)
    active: dict[Future[dict[str, Any]], str] = {}
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="history-breadth")
    try:
        while pending_days or active:
            while pending_days and len(active) < 4 and time.monotonic() < deadline:
                day = pending_days.pop(0)
                timeout = min(_REQUEST_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
                active[executor.submit(_download_breadth_payload, day, timeout)] = day
            if not active:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(active, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                day = active.pop(future)
                try:
                    payload = future.result()
                    parsed = _parse_breadth_payload(payload, day)
                    _write_json_atomic(
                        cache_root / f"kaipanhong-{day}.json",
                        {
                            "version": _CACHE_VERSION,
                            "kind": "kaipanhong_day",
                            "day": day,
                            "fetched_at": datetime.now().astimezone().isoformat(),
                            "payload": payload,
                        },
                    )
                    output[day] = parsed
                    source["downloaded_days"] += 1
                except (OSError, ProviderDataError, ProviderTransportError, ValueError) as error:
                    errors.append(f"开盘红 {day}：{error}")
    finally:
        unfinished = [*active.values(), *pending_days]
        for future in active:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    for day in unfinished:
        errors.append(f"开盘红 {day}：超过总截止时间")


def _download_breadth_payload(day: str, timeout_seconds: float) -> dict[str, Any]:
    form = dict(_KAIPANHONG_BASE_FORM)
    form["Day"] = day
    try:
        response = requests.post(
            _KAIPANHONG_URL,
            headers=_KAIPANHONG_HEADERS,
            data=form,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise ProviderTransportError(str(error)) from error
    except ValueError as error:
        raise ProviderDataError("响应不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise ProviderDataError("响应 JSON 顶层不是对象")
    return payload


def _parse_breadth_payload(payload: dict[str, Any], expected_day: str) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise ProviderDataError("响应 JSON 顶层不是对象")
    if str(payload.get("errcode")) != "0":
        raise ProviderDataError(f"errcode={payload.get('errcode')!r}")
    response_day = payload.get("date")
    if response_day != expected_day:
        raise ProviderDataError(
            f"响应日期错位：请求 {expected_day}，返回 {response_day!r}"
        )
    info = payload.get("info")
    if not isinstance(info, dict) or not info:
        raise ProviderDataError("info 为空或不是对象")
    values = {
        "up_count": _nonnegative_integer(info.get("SZJS"), "SZJS"),
        "down_count": _nonnegative_integer(info.get("XDJS"), "XDJS"),
        "flat_count": _nonnegative_integer(info.get("0"), "0"),
        "limit_up": _nonnegative_integer(info.get("ZT"), "ZT"),
        "limit_down": _nonnegative_integer(info.get("DT"), "DT"),
    }
    valid_stock_count = values["up_count"] + values["down_count"] + values["flat_count"]
    if valid_stock_count <= 0:
        raise ProviderDataError("上涨、下跌、平盘总数必须大于 0")
    values["valid_stock_count"] = valid_stock_count
    return values


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ProviderDataError(f"字段 {field} 不是非负整数")
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ProviderDataError(f"字段 {field} 不是非负整数") from None
    if not isfinite(number) or number < 0 or not number.is_integer():
        raise ProviderDataError(f"字段 {field} 不是非负整数")
    return int(number)


def _positive_number(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _derive_official_expiries(
    months: dict[str, dict[str, list[dict[str, Any]]]], complete_months: set[str]
) -> dict[str, str]:
    expiries: dict[str, str] = {}
    for month in complete_months:
        symbol = f"IF{month[2:]}"
        appearances = [
            day
            for day, contracts in months.get(month, {}).items()
            if any(contract.get("symbol") == symbol for contract in contracts)
        ]
        if appearances:
            last_seen = date.fromisoformat(max(appearances))
            expected = contract_expiry(symbol)
            if expected <= last_seen <= expected + timedelta(days=10):
                expiries[symbol] = last_seen.isoformat()
    return expiries


def _annotate_official_expiries(
    futures: dict[str, list[dict[str, Any]]], expiries: dict[str, str]
) -> None:
    for contracts in futures.values():
        for contract in contracts:
            expiry = expiries.get(str(contract.get("symbol")))
            if expiry is not None:
                contract["expiry"] = expiry


def _compact_errors(errors: list[str]) -> list[str]:
    if len(errors) <= _MAX_REPORTED_ERRORS:
        return errors
    omitted = len(errors) - _MAX_REPORTED_ERRORS
    return [*errors[:_MAX_REPORTED_ERRORS], f"另有 {omitted} 条错误未列出"]


def _cache_root() -> Path:
    configured = os.environ.get("PANIC_INDEX_CACHE_DIRECTORY")
    if not configured:
        raise ProviderUnavailable("未配置 PANIC_INDEX_CACHE_DIRECTORY，无法缓存历史缺项")
    root = Path(configured).expanduser().resolve() / "history-extra"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_cffex_cache(
    path: Path, expected_month: str
) -> tuple[dict[str, list[dict[str, Any]]], bool] | None:
    payload = _read_json(path)
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _CACHE_VERSION
        or payload.get("kind") != "cffex_month"
        or payload.get("month") != expected_month
        or not isinstance(payload.get("days"), dict)
    ):
        return None
    normalized: dict[str, list[dict[str, Any]]] = {}
    try:
        for day, contracts in payload["days"].items():
            parsed_day = date.fromisoformat(str(day)).isoformat()
            if parsed_day != day or day[:7].replace("-", "") != expected_month:
                return None
            if not isinstance(contracts, list) or not contracts:
                return None
            parsed_contracts: list[dict[str, Any]] = []
            for contract in contracts:
                if not isinstance(contract, dict):
                    return None
                symbol = str(contract.get("symbol") or "").upper()
                last = _positive_number(contract.get("last"))
                if not _IF_CONTRACT_PATTERN.fullmatch(symbol) or last is None:
                    return None
                parsed_contracts.append({"symbol": symbol, "last": last})
            normalized[day] = sorted(parsed_contracts, key=lambda item: item["symbol"])
    except (TypeError, ValueError):
        return None
    if not normalized:
        return None
    return normalized, payload.get("complete") is True


def _read_breadth_cache(path: Path, expected_day: str) -> dict[str, int] | None:
    payload = _read_json(path)
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _CACHE_VERSION
        or payload.get("kind") != "kaipanhong_day"
        or payload.get("day") != expected_day
        or not isinstance(payload.get("payload"), dict)
    ):
        return None
    try:
        return _parse_breadth_payload(payload["payload"], expected_day)
    except ProviderDataError:
        return None


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
