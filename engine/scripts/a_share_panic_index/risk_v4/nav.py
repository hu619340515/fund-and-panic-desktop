"""公开基金代码的真实单位净值获取。

不从用户上传的持仓数据读取净值；累计净值或日涨跌幅都不能被反向累计为
总回报。分红、拆分资料不能核实时明确返回不完整状态。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from itertools import pairwise
from math import isfinite, prod
from typing import Any

from .providers import resolve_unique_fund_benchmark

_SOURCE = {
    "provider": "akshare",
    "upstream": "eastmoney_public_fund",
    "dataset": "单位净值走势",
    "source_url": "https://fund.eastmoney.com/",
}


def fetch_fund_history(code: str, *, timeout: float = 15) -> dict[str, Any]:
    """读取公开基金单位净值，并保守声明总回报完整性。"""
    normalized = _fund_code(code)
    fetched_at = _now()
    if normalized is None:
        return _unavailable(str(code), fetched_at, "基金代码必须为六位数字字符串")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        return _unavailable(normalized, fetched_at, "timeout 必须为正数")
    try:
        ak = _load_akshare()
        # AKShare 仅作为免费公开数据适配层；该调用不使用任何用户持仓数据。
        frame = ak.fund_open_fund_info_em(symbol=normalized, indicator="单位净值走势")
    except Exception as exc:  # noqa: BLE001 - 第三方公开源不保证异常类型
        return _unavailable(normalized, fetched_at, f"公开单位净值获取失败：{exc}")
    rows, diagnostics = _unit_nav_rows(frame)
    if not rows:
        return _unavailable(normalized, fetched_at, "上游未返回可验证的单位净值", diagnostics)
    distributions = _fetch_events(ak, normalized, "分红送配详情", "distribution")
    splits = _fetch_events(ak, normalized, "拆分详情", "split")
    total_return = compute_total_returns(rows, distributions, splits)
    return {
        "code": normalized,
        "available": True,
        "history": rows,
        "unit_nav": rows,
        "events": {"distributions": distributions, "splits": splits},
        "total_return": total_return,
        "completeness": {
            "unit_nav_verified": True,
            "distribution_events_verified": distributions["available"] and not distributions["unresolved"],
            "split_events_verified": splits["available"] and not splits["unresolved"],
            "total_return_verified": total_return["available"],
        },
        "source": dict(_SOURCE),
        "published_at": None,
        "fetched_at": fetched_at,
        "diagnostics": diagnostics + distributions["diagnostics"] + splits["diagnostics"] + ["未使用累计净值或日涨跌幅反推总回报"],
        "raw_records":json.loads(frame.to_json(orient="records", date_format="iso")),
    }


def fetch_fund_metadata(code: str, *, timeout: float = 15) -> dict[str, Any]:
    """读取公开销售资料，只有 ETF 联接的明确跟踪指数才自动映射代码。"""
    normalized = _fund_code(code)
    fetched_at = _now()
    if normalized is None:
        return _metadata_unverified(str(code), fetched_at, "基金代码必须为六位数字字符串")
    try:
        frame = _load_akshare().fund_individual_basic_info_xq(symbol=normalized, timeout=timeout)
        record = _metadata_pairs(frame)
    except Exception as exc:  # noqa: BLE001 - 第三方公开源不保证异常类型
        return _metadata_unverified(normalized, fetched_at, f"公开元数据获取失败：{exc}")
    if not record:
        return _metadata_unverified(normalized, fetched_at, "公开元数据未找到该基金")
    name = _pick(record, "基金名称", "基金简称", "名称")
    benchmark = _pick(record, "业绩比较基准")
    fund_type = _pick(record, "基金类型")
    investment_target = _pick(record, "投资目标", "投资策略")
    full_name = _pick(record, "基金全称")
    mapping = resolve_unique_fund_benchmark(
        fund_type,
        benchmark,
        " ".join(str(value or "") for value in (name, full_name, investment_target, _pick(record, "投资策略"))),
    )
    symbol = mapping.get("symbol")
    asset_class = _asset_class(fund_type)
    return {
        "code": normalized,
        "verified": symbol is not None,
        "metadata_verified": bool(name and benchmark),
        "name": name,
        "symbol": symbol,
        "benchmark_symbol": symbol,
        "benchmark_candidates": [str(benchmark)] if benchmark else [],
        "benchmark_mapping": mapping,
        "asset_class": asset_class,
        "fund_type":str(fund_type or ""),
        "raw_record":{str(key):str(value) for key,value in record.items()},
        "industry_candidates": [],
        "source": {
            "provider": "akshare",
            "upstream": "danjuanfunds_public_fund_profile",
            "dataset": "基金概况/业绩比较基准",
            "source_url": f"https://danjuanfunds.com/funding/{normalized}",
        },
        "published_at": None,
        "fetched_at": fetched_at,
        "diagnostics": _metadata_diagnostics(symbol, benchmark, asset_class) + (["指数映射是当前公开资料核验，不证明历史时点可得性"] if symbol else []),
    }


def compute_total_returns(
    unit_nav: list[dict[str, Any]], distributions: dict[str, Any] | list[dict[str, Any]], splits: dict[str, Any] | list[dict[str, Any]]
) -> dict[str, Any]:
    """用单位净值与已核实的除息/拆分事件构造分段真实总回报。

    查询成功但事件表为空代表“已核实无事件”；查询失败与无法解析的事件则形成
    缺口，而不是把整个序列或日涨跌幅伪装为完整总回报。
    """
    distributions = _event_source(distributions)
    splits = _event_source(splits)
    if not distributions.get("available") or not splits.get("available"):
        return {
            "available": False,
            "partial": False,
            "series": [],
            "gaps": [{"reason": "分红或拆分事件表不可得"}],
            "reason": "分红或拆分事件表不可得，无法核实总回报",
        }
    rows = sorted(
        [row for row in unit_nav if _date_text(row.get("trade_date")) and _positive(row.get("unit_nav"))],
        key=lambda row: row["trade_date"],
    )
    if not rows:
        return {"available": False, "partial": False, "series": [], "gaps": [], "reason": "没有有效单位净值"}
    unresolved = list(distributions.get("unresolved") or []) + list(splits.get("unresolved") or [])
    if any(not item.get("effective_date") for item in unresolved):
        return {
            "available": False,
            "partial": False,
            "series": [],
            "gaps": unresolved,
            "reason": "存在日期无法核实的分红或拆分事件",
        }
    distributions_by_day = _events_by_effective_day(distributions.get("events") or [])
    splits_by_day = _events_by_effective_day(splits.get("events") or [])
    unresolved_by_day = _events_by_effective_day(unresolved)
    output = [{"trade_date": rows[0]["trade_date"], "total_return": 1.0, "segment": 0}]
    gaps: list[dict[str, Any]] = []
    cumulative = 1.0
    segment = 0
    nav_days = {row["trade_date"] for row in rows}
    missing_event_days = {
        day for day in set(distributions_by_day) | set(splits_by_day) | set(unresolved_by_day)
        if rows[0]["trade_date"] < day <= rows[-1]["trade_date"] and day not in nav_days
    }
    for previous, current in pairwise(rows):
        effective_day = current["trade_date"]
        crossed = sorted(day for day in missing_event_days if previous["trade_date"] < day <= effective_day)
        distribution_events = distributions_by_day.get(effective_day, [])
        split_events = splits_by_day.get(effective_day, [])
        # 同日多笔事件与拆分兼派息时，公开表不能证明现金属于拆分前还是拆分后份额。
        ambiguous = (len(distribution_events) > 1 or len(split_events) > 1 or
                     bool(distribution_events and split_events))
        if effective_day in unresolved_by_day or crossed or ambiguous:
            gaps.extend(unresolved_by_day.get(effective_day, []))
            gaps.extend({"effective_date": day, "reason": "事件生效日缺少单位净值，无法核实调整"} for day in crossed)
            if ambiguous:
                gaps.append({"effective_date": effective_day, "reason": "同日多笔事件或拆分兼派息，调整先后及单位不可核实"})
            segment += 1
            cumulative = 1.0
            output.append({"trade_date": effective_day, "period_start": previous["trade_date"],
                           "total_return": cumulative, "segment": segment, "gap_before": True})
            continue
        cash = sum(float(event["cash_per_unit"]) for event in distribution_events)
        ratio = prod(float(event["ratio"]) for event in split_events)
        factor = (float(current["unit_nav"]) * ratio + cash) / float(previous["unit_nav"])
        if not isfinite(factor) or factor <= 0:
            gaps.append({"effective_date": effective_day, "reason": "事件调整后总回报因子无效"})
            segment += 1
            cumulative = 1.0
            output.append({"trade_date": effective_day, "period_start": previous["trade_date"],
                           "total_return": cumulative, "segment": segment, "gap_before": True})
            continue
        cumulative *= factor
        output.append({"trade_date": effective_day, "period_start": previous["trade_date"],
                       "total_return": cumulative, "return":factor-1.0, "segment": segment})
    return {
        "available": not gaps,
        "partial": bool(gaps),
        "series": output,
        "gaps": gaps,
        "reason": None if not gaps else "部分事件无法核实，序列已按缺口分段",
    }


def _event_source(value: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(value, list):
        return {"available": True, "events": value, "unresolved": []}
    return value if isinstance(value, dict) else {"available": False, "events": [], "unresolved": []}


def _load_akshare():
    import akshare

    return akshare


def _unit_nav_rows(frame: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return [], ["上游返回不是表格"]
    rows: list[dict[str, Any]] = []
    skipped = 0
    for _, item in frame.iterrows():
        raw = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        day = _date_text(_pick(raw, "净值日期", "日期", "date"))
        nav = _positive(_pick(raw, "单位净值", "unit_nav", "nav"))
        if day is None or nav is None:
            skipped += 1
            continue
        rows.append({"trade_date": day, "unit_nav": nav})
    unique = {row["trade_date"]: row for row in rows}
    if any(unique[row["trade_date"]]["unit_nav"] != row["unit_nav"] for row in rows):
        return [], ["同一净值日期有互相冲突的单位净值，拒绝择一复权"]
    rows = [unique[day] for day in sorted(unique)]
    diagnostics = [f"跳过 {skipped} 条日期或单位净值无效记录"] if skipped else []
    if len(rows) > 1:
        diagnostics.append("单位净值序列的交易日连续性未由公开接口证明")
    return rows, diagnostics


def _fetch_events(ak: Any, code: str, indicator: str, kind: str) -> dict[str, Any]:
    try:
        frame = ak.fund_open_fund_info_em(symbol=code, indicator=indicator)
    except Exception as exc:  # noqa: BLE001 - 第三方公开源不保证异常类型
        return {"available": False, "events": [], "unresolved": [], "diagnostics": [f"{indicator} 获取失败：{exc}"], "reason": "公开事件表不可得"}
    if frame is None or not hasattr(frame, "iterrows"):
        return {"available": False, "events": [], "unresolved": [], "diagnostics": [f"{indicator} 返回不是表格"], "reason": "公开事件表不可得"}
    if getattr(frame, "empty", False):
        return {"available": True, "events": [], "unresolved": [], "diagnostics": [f"{indicator} 已查询且无事件"], "reason": None}
    events: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for _, item in frame.iterrows():
        record = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        parsed = _parse_distribution(record) if kind == "distribution" else _parse_split(record)
        if parsed.get("valid"):
            parsed.pop("valid", None)
            events.append(parsed)
        else:
            parsed.pop("valid", None)
            unresolved.append(parsed)
    diagnostics = []
    if unresolved:
        diagnostics.append(f"{indicator} 有 {len(unresolved)} 条事件无法完整解析")
    return {"available": True, "events": events, "unresolved": unresolved, "diagnostics": diagnostics, "reason": None}


def _parse_distribution(record: dict[str, Any]) -> dict[str, Any]:
    # 登记日、发放日和普通“日期”均不能替代真正的除息生效日。
    day = _date_text(_pick(record, "除息日", "除权除息日"))
    cash = _distribution_cash(record)
    return {
        "valid": day is not None and cash is not None and cash >= 0,
        "effective_date": day,
        "cash_per_unit": cash,
        "reason": None if day is not None and cash is not None and cash >= 0 else "分红事件缺少可核实的除息日或每份现金",
        "raw": {str(key): str(value) for key, value in record.items()},
    }


def _parse_split(record: dict[str, Any]) -> dict[str, Any]:
    day = _date_text(_pick(record, "拆分折算日", "折算日", "除权日"))
    raw = _pick(record, "拆分比例", "折算比例", "拆分折算比例", "拆分方案")
    ratio = _split_ratio(raw)
    return {
        "valid": day is not None and ratio is not None and ratio > 0,
        "effective_date": day,
        "ratio": ratio,
        "reason": None if day is not None and ratio is not None and ratio > 0 else "拆分事件缺少可核实的生效日或份额比例",
        "raw": {str(key): str(value) for key, value in record.items()},
    }


def _distribution_cash(record: dict[str, Any]) -> float | None:
    text_candidates = []
    for field, divisor in (("每10份派现金", 10.0), ("每10份基金份额派现金", 10.0),
                           ("每10份分红", 10.0), ("每份派现金", 1.0)):
        if field in record:
            number = _positive(record[field])
            if number is not None:
                return number / divisor
            # 标题已有单位时，仅接受金额自身，不读取“每10份”中的10。
            match = re.fullmatch(r"\s*(?:人民币|现金|派现金)?\s*([\d,]+(?:\.\d+)?)\s*(?:元)?\s*", str(record[field]))
            if match:
                amount = _positive(match.group(1).replace(",", ""))
                return amount / divisor if amount is not None else None
            text_candidates.append(str(record[field]))
    text_candidates.append(str(record.get("分红方案") or ""))
    for text in text_candidates:
        match = re.search(r"每\s*(10|1)\s*份(?:基金份额)?\s*(?:派|分配|分红)\s*(?:现金)?\s*([\d,]+(?:\.\d+)?)\s*元?", text)
        if match:
            amount = _positive(match.group(2).replace(",", ""))
            return amount / float(match.group(1)) if amount is not None else None
        match = re.search(r"每\s*份(?:基金份额)?\s*(?:派|分配|分红)\s*(?:现金)?\s*([\d,]+(?:\.\d+)?)\s*元?", text)
        if match:
            return _positive(match.group(1).replace(",", ""))
    return None


def _split_ratio(value: Any) -> float | None:
    if value is None:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if len(numbers) == 1:
        return _positive(numbers[0])
    if len(numbers) >= 2 and "拆分" in str(value):
        before, after = float(numbers[0]), float(numbers[1])
        return after / before if before > 0 and after > 0 else None
    return None


def _events_by_effective_day(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        day = event.get("effective_date")
        if day:
            output.setdefault(str(day), []).append(event)
    return output


def _metadata_pairs(frame: Any) -> dict[str, Any]:
    if frame is None or not hasattr(frame, "iterrows"):
        return {}
    output: dict[str, Any] = {}
    for _, item in frame.iterrows():
        row = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        key, value = row.get("item"), row.get("value")
        if key is not None:
            output[str(key).strip()] = value
    return output


def _asset_class(fund_type: Any) -> str | None:
    text = str(fund_type or "")
    if "股票" in text or "指数" in text:
        return "equity"
    if "债" in text:
        return "bond"
    if "货币" in text:
        return "money"
    return None


def _metadata_diagnostics(symbol: str | None, benchmark: Any, asset_class: str | None) -> list[str]:
    notes = []
    if benchmark:
        notes.append(f"公开业绩比较基准：{benchmark}")
    else:
        notes.append("公开资料未提供业绩比较基准")
    if symbol is None:
        notes.append("仅 ETF 联接且基准唯一匹配时自动映射指数代码；其余须人工确认")
    if asset_class is None:
        notes.append("基金资产类别未能从公开类型字段核实")
    return notes


def _find_metadata_record(frame: Any, code: str) -> dict[str, Any] | None:
    if frame is None or not hasattr(frame, "iterrows"):
        return None
    for _, item in frame.iterrows():
        record = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        candidate = str(_pick(record, "基金代码", "代码", "code") or "").strip()
        if candidate.zfill(6) == code and candidate.isdigit():
            return record
    return None


def _pick(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip() not in {"", "nan", "None"}:
            return value
    return None


def _fund_code(value: Any) -> str | None:
    code = str(value).strip()
    return code if code.isascii() and code.isdigit() and len(code) == 6 else None


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _unavailable(code: str, fetched_at: str, reason: str, diagnostics: list[str] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "available": False,
        "history": [],
        "unit_nav": [],
        "events": _unverified_events(reason),
        "total_return": {"available": False, "series": [], "reason": reason},
        "completeness": {"unit_nav_verified": False, "distribution_events_verified": False, "split_events_verified": False, "total_return_verified": False},
        "source": dict(_SOURCE),
        "published_at": None,
        "fetched_at": fetched_at,
        "diagnostics": (diagnostics or []) + [reason],
    }


def _metadata_unverified(code: str, fetched_at: str, reason: str) -> dict[str, Any]:
    return {
        "code": code,
        "verified": False,
        "metadata_verified": False,
        "name": None,
        "symbol": None,
        "benchmark_symbol": None,
        "benchmark_candidates": [],
        "asset_class": None,
        "industry_candidates": [],
        "source": {"provider": "akshare", "upstream": "eastmoney_public_fund", "dataset": "基金名称列表"},
        "published_at": None,
        "fetched_at": fetched_at,
        "diagnostics": [reason, "不猜测基金基准或行业"],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unverified_events(reason: str = "公开单位净值接口未提供可核实事件表") -> dict[str, Any]:
    return {
        "distributions": {"available": False, "events": [], "reason": reason},
        "splits": {"available": False, "events": [], "reason": reason},
    }
