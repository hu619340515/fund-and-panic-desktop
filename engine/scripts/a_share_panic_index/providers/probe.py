"""数据源能力探测与覆盖报告。"""

from __future__ import annotations

import csv
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from ..features.derivatives import select_if_contracts
from ..models import ProbeResult
from .base import HttpClient, ProviderDataError, run_with_hard_timeout, shanghai_now
from .live import fetch_live


PROBE_TARGETS = [
    ("eastmoney", "index", "东方财富指数实时"),
    ("eastmoney", "breadth", "东方财富全市场实时"),
    ("mootdx", "index", "mootdx在线指数"),
    ("tencent", "index", "腾讯指数实时"),
    ("tencent", "minute", "腾讯指数分钟线"),
    ("sina", "index", "新浪指数实时"),
    ("sina", "minute", "新浪指数5分钟线"),
    ("baostock", "proxy_curve", "BaoStock 510300 5分钟"),
    ("xuangubao", "limits", "选股宝涨停详情"),
    ("qvix_300_index", "qvix", "300股指QVIX"),
    ("qvix_300_etf", "qvix", "300ETF QVIX"),
    ("mootdx", "futures", "mootdx明确IF合约"),
    ("sina_futures", "futures", "新浪明确IF合约"),
    ("akshare_futures", "futures", "AKShare中金所IF合约"),
]


def run_source_probe(
    settings,
    database,
    output: Path,
    fixture: str | Path | None = None,
) -> dict[str, Any]:
    tested_at = shanghai_now()
    if fixture:
        results = _load_fixture(Path(fixture))
        mode = "fixture"
    else:
        context = {
            "now": tested_at.isoformat(),
            "trade_date": tested_at.date().isoformat(),
            "symbol": settings.get("market.index_symbol"),
            "timeout": float(settings.get("network.provider_timeout_seconds")),
        }
        timeout = float(settings.get("network.provider_timeout_seconds"))
        results = []
        for provider, semantic, endpoint in PROBE_TARGETS:
            started = time.perf_counter()
            try:
                raw = run_with_hard_timeout(
                    probe_worker,
                    (provider, semantic, endpoint, context),
                    timeout,
                )
                results.append(raw)
            except Exception as error:
                results.append(
                    ProbeResult(
                        provider=provider,
                        endpoint_or_function=endpoint,
                        semantic_type=semantic,
                        available=False,
                        latency_ms=round((time.perf_counter() - started) * 1000, 3),
                        returned_rows=0,
                        earliest_timestamp=None,
                        latest_timestamp=None,
                        fields=[],
                        units={},
                        source_timestamp=None,
                        supports_realtime=semantic in {"index", "breadth", "limits", "futures", "qvix"},
                        supports_1m=semantic == "minute" and provider == "tencent",
                        supports_5m=semantic in {"minute", "proxy_curve"},
                        supports_history=semantic in {"minute", "proxy_curve", "qvix"},
                        maximum_observed_rows=0,
                        requires_cookie=False,
                        requires_login=provider == "baostock",
                        error=str(error),
                        tested_at=tested_at.isoformat(),
                    ).to_dict()
                )
        mode = "live"
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "3.0",
        "probe_mode": mode,
        "tested_at": tested_at.isoformat(),
        "results": results,
    }
    with output.open("w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
    coverage_path = output.parent / "source_coverage.csv"
    disagreement_path = output.parent / "source_disagreements.csv"
    _write_coverage(coverage_path, results)
    _write_disagreements(disagreement_path, results)
    database.save_probe_results(results)
    return {
        **payload,
        "output": str(output),
        "coverage_csv": str(coverage_path),
        "disagreements_csv": str(disagreement_path),
    }


def probe_worker(
    provider: str,
    semantic: str,
    endpoint: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    tested_at = shanghai_now()
    if semantic == "minute":
        details = _probe_minute(provider, context)
    elif provider == "baostock":
        details = _probe_baostock(context)
    else:
        result = fetch_live(provider, semantic, context)
        data = result["data"]
        details = {
            "returned_rows": int(data.get("returned_rows", 1)),
            "earliest_timestamp": data.get("earliest_timestamp", result["source_timestamp"]),
            "latest_timestamp": data.get("latest_timestamp", result["source_timestamp"]),
            "fields": sorted(data.keys()),
            "source_timestamp": result["source_timestamp"],
            "maximum_observed_rows": int(data.get("returned_rows", 1)),
        }
    units = _units_for(semantic)
    supports_realtime = semantic in {"index", "breadth", "limits", "futures", "qvix"}
    payload = ProbeResult(
        provider=provider,
        endpoint_or_function=endpoint,
        semantic_type=semantic,
        available=True,
        latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
        returned_rows=details["returned_rows"],
        earliest_timestamp=details.get("earliest_timestamp"),
        latest_timestamp=details.get("latest_timestamp"),
        fields=details["fields"],
        units=units,
        source_timestamp=details.get("source_timestamp"),
        supports_realtime=supports_realtime,
        supports_1m=semantic == "minute" and provider == "tencent",
        supports_5m=semantic in {"minute", "proxy_curve"},
        supports_history=semantic in {"minute", "proxy_curve", "qvix"},
        maximum_observed_rows=details["maximum_observed_rows"],
        requires_cookie=False,
        requires_login=provider == "baostock",
        error=None,
        tested_at=tested_at.isoformat(),
    ).to_dict()
    if semantic not in {"minute", "proxy_curve"}:
        sample_value, sample_key = _sample_value(
            semantic, data, context.get("trade_date")
        )
        payload["sample_value"] = sample_value
        payload["sample_key"] = sample_key
    return payload


def _probe_minute(provider: str, context: dict[str, Any]) -> dict[str, Any]:
    client = HttpClient(context.get("timeout", 20))
    if provider == "sina":
        response = client.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": "sh000300", "scale": 5, "ma": 5, "datalen": 20},
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ProviderDataError("新浪分钟线为空")
        timestamps = [str(row.get("day")) for row in rows if row.get("day")]
        fields = sorted({key for row in rows for key in row})
    elif provider == "tencent":
        response = client.get(
            "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
            params={"param": "sh000300,m5,,20"},
        )
        payload = response.json()
        node = (payload.get("data") or {}).get("sh000300") or {}
        rows = node.get("m5") or []
        if not rows:
            raise ProviderDataError("腾讯分钟线为空")
        timestamps = [str(row[0]) for row in rows if row]
        fields = ["时间", "开盘", "收盘", "最高", "最低", "成交量"]
    else:
        raise ProviderDataError(f"不支持的分钟探测源: {provider}")
    return {
        "returned_rows": len(rows),
        "earliest_timestamp": min(timestamps) if timestamps else None,
        "latest_timestamp": max(timestamps) if timestamps else None,
        "fields": fields,
        "source_timestamp": max(timestamps) if timestamps else None,
        "maximum_observed_rows": len(rows),
    }


def _probe_baostock(context: dict[str, Any]) -> dict[str, Any]:
    result = fetch_live("baostock", "proxy_curve", context)
    data = result["data"]
    return {
        "returned_rows": int(data["returned_rows"]),
        "earliest_timestamp": data.get("earliest_timestamp"),
        "latest_timestamp": data.get("latest_timestamp"),
        "fields": ["代码", "日期", "5分钟时间桶", "成交额", "累计成交比例"],
        "source_timestamp": result["source_timestamp"],
        "maximum_observed_rows": int(data["returned_rows"]),
    }


def _load_fixture(path: Path) -> list[dict[str, Any]]:
    target = path / "results.json" if path.is_dir() else path
    if not target.exists():
        raise FileNotFoundError(f"数据源探测夹具不存在: {target}")
    with target.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    results = payload.get("results", payload)
    if not isinstance(results, list) or not results:
        raise ValueError("数据源探测夹具必须包含非空results数组")
    required = set(ProbeResult.__dataclass_fields__)
    for result in results:
        missing = required - set(result)
        if missing:
            raise ValueError("探测夹具缺少字段: " + ", ".join(sorted(missing)))
    return results


def _units_for(semantic: str) -> dict[str, str]:
    return {
        "index": {"price": "指数点", "amount": "元"},
        "breadth": {"change_percent": "小数", "market_amount": "元"},
        "limits": {"limit_up": "家", "limit_down": "家"},
        "futures": {"price": "指数点", "basis": "年化小数"},
        "qvix": {"value": "波动率指数点"},
        "minute": {"price": "指数点", "volume": "手"},
        "proxy_curve": {"amount": "元", "cumulative_share": "小数"},
    }.get(semantic, {})


def _write_coverage(path: Path, results: list[dict[str, Any]]) -> None:
    headers = ["数据源", "金融语义", "是否可用", "最早时间", "最新时间", "返回行数", "延迟毫秒", "错误"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "数据源": item["provider"],
                    "金融语义": item["semantic_type"],
                    "是否可用": "是" if item["available"] else "否",
                    "最早时间": item.get("earliest_timestamp") or "",
                    "最新时间": item.get("latest_timestamp") or "",
                    "返回行数": item.get("returned_rows", 0),
                    "延迟毫秒": item.get("latency_ms", 0),
                    "错误": item.get("error") or "",
                }
            )


def _write_disagreements(path: Path, results: list[dict[str, Any]]) -> None:
    headers = ["金融语义", "数据源A", "数据源B", "差异比例", "说明"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        available = [item for item in results if item.get("available")]
        by_semantic: dict[str, list[dict[str, Any]]] = {}
        for item in available:
            by_semantic.setdefault(item["semantic_type"], []).append(item)
        for semantic, items in by_semantic.items():
            valued = [item for item in items if item.get("sample_value") is not None]
            if len(valued) >= 2:
                first = valued[0]
                for second in valued[1:]:
                    if first.get("sample_key") != second.get("sample_key"):
                        continue
                    left = float(first["sample_value"])
                    right = float(second["sample_value"])
                    difference = abs(left - right) / max(abs(left), abs(right), 1e-9)
                    writer.writerow(
                        {
                            "金融语义": semantic,
                            "数据源A": first["provider"],
                            "数据源B": second["provider"],
                            "差异比例": f"{difference:.8f}",
                            "说明": "同轮真实探测样本值相对差异",
                        }
                    )
            elif len(items) >= 2:
                writer.writerow(
                    {
                        "金融语义": semantic,
                        "数据源A": items[0]["provider"],
                        "数据源B": items[1]["provider"],
                        "差异比例": "",
                        "说明": "探测仅验证能力；实时值差异由采集管线记录",
                    }
                )


def _sample_value(
    semantic: str,
    data: dict[str, Any],
    trade_date: str | None,
) -> tuple[float | None, str | None]:
    try:
        if semantic == "index":
            return float(data["last"]), str(data.get("symbol") or "index")
        if semantic == "qvix":
            return float(data["value"]), str(data.get("symbol") or "qvix")
        if semantic == "futures":
            contracts = data.get("contracts", [])
            if not contracts:
                return None, None
            target = date.fromisoformat(str(trade_date))
            contract, _ = select_if_contracts(contracts, target, 5)
            bid = contract.get("bid")
            ask = contract.get("ask")
            if bid is not None and ask is not None:
                return (float(bid) + float(ask)) / 2.0, str(contract["symbol"])
            return float(contract.get("last", contract.get("price"))), str(
                contract["symbol"]
            )
    except (KeyError, TypeError, ValueError):
        return None, None
    return None, None
