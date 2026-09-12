"""基金组合导入、校验及保守的风险建议。

本模块只生成需要人工确认的建议，不会下单，也不会把未知的费用、份额或
交易日历伪装成精确交易金额。
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np

_PROFILES = {
    "conservative": {"equity_cap": 0.60, "vol_target": 0.06},
    "balanced": {"equity_cap": 0.80, "vol_target": 0.10},
    "aggressive": {"equity_cap": 0.95, "vol_target": 0.14},
}
_DEFAULT_CONSTRAINTS = {
    "single_fund_cap": 0.20,
    "industry_cap": 0.35,
    "daily_change_cap": 0.10,
    "no_trade_band": 0.05,
}
_CONSTRAINT_KEYS = {
    "equity_cap",
    "vol_target",
    "single_fund_cap",
    "industry_cap",
    "daily_change_cap",
    "no_trade_band",
}
_CSV_HEADERS = {
    "基金代码": "code",
    "持有份额": "shares",
    "持有市值": "market_value",
    "估值日期": "valuation_date",
    "申购确认日期": "confirmed_date",
    "基准权重": "baseline_weight",
    "行业": "industry",
    "申购费率": "fee_buy",
    "赎回费率": "fee_sell",
    "在途金额": "in_transit",
    "基准代码": "benchmark_symbol",
    "资产类别": "asset_class",
    "元数据已核实": "metadata_verified",
    "生效日期": "effective_date",
}


def validate_portfolio(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并标准化持仓输入，返回可安全持久化的对象。"""
    errors: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return {"valid": False, "errors": [_error("payload", "必须是对象")], "portfolio": None}

    profile = payload.get("profile", "balanced")
    if profile not in _PROFILES:
        errors.append(_error("profile", "仅支持 conservative、balanced 或 aggressive"))
        profile = "balanced"
    version = payload.get("version", 1)
    if version != 1:
        errors.append(_error("version", "仅支持版本 1"))

    cash = _number(payload.get("cash", 0), "cash", errors, minimum=0)
    lots_payload = payload.get("lots", [])
    if not isinstance(lots_payload, list):
        errors.append(_error("lots", "必须是数组"))
        lots_payload = []
    lots = [_normalize_lot(item, index, errors) for index, item in enumerate(lots_payload)]
    lots = [item for item in lots if item is not None]
    ids: set[str] = set()
    metadata_by_code: dict[str, tuple[str, str, str | None]] = {}
    for index, lot in enumerate(lots):
        if lot["id"] in ids:
            errors.append(_error(f"lots[{index}].id", "批次 ID 重复"))
        ids.add(lot["id"])
        if lot["metadata_verified"]:
            identity = (lot["benchmark_symbol"], lot["asset_class"], lot["industry"])
            existing = metadata_by_code.setdefault(lot["code"], identity)
            if existing != identity:
                errors.append(_error(f"lots[{index}]", "同一基金的已核实基准、资产类别或行业互相冲突"))
    if not lots and (cash is None or cash <= 0):
        errors.append(_error("lots", "至少提供一笔持仓或正现金"))

    constraints = _normalize_constraints(payload.get("constraints"), profile, errors)
    _validate_constraint_relationships(constraints, errors)
    normalized = {
        "version": 1,
        "cash": cash if cash is not None else 0.0,
        "profile": profile,
        "lots": lots,
        "constraints": constraints,
    }
    return {"valid": not errors, "errors": errors, "portfolio": normalized if not errors else None}


def import_csv(text: str) -> dict[str, Any]:
    """导入中文表头 CSV；只预览并校验，不写入用户数据库。"""
    if not isinstance(text, str) or not text.strip():
        return {"valid": False, "errors": [_error("csv", "CSV 内容为空")], "rows": [], "portfolio": None}
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        headers = {str(header).strip() for header in (reader.fieldnames or []) if header}
    except csv.Error as exc:
        return {"valid": False, "errors": [_error("csv", f"无法解析 CSV：{exc}")], "rows": [], "portfolio": None}
    required = {"基金代码", "估值日期"}
    missing = sorted(required - headers)
    if missing:
        return {"valid": False, "errors": [_error("csv", f"缺少中文表头：{'、'.join(missing)}")], "rows": [], "portfolio": None}
    unknown = sorted(headers - set(_CSV_HEADERS))
    errors = [_error("csv", f"不支持的表头：{'、'.join(unknown)}")] if unknown else []
    lots: list[dict[str, Any]] = []
    try:
        for row_index, source in enumerate(reader, start=2):
            if not any(str(value or "").strip() for value in source.values()):
                continue
            lot = {"id": f"csv-{row_index}"}
            for title, key in _CSV_HEADERS.items():
                value = source.get(title)
                lot[key] = value.strip() if isinstance(value, str) else value
            lots.append(lot)
    except csv.Error as exc:
        errors.append(_error("csv", f"读取第 {len(lots) + 2} 行失败：{exc}"))
    checked = validate_portfolio({"version": 1, "cash": 0, "profile": "balanced", "lots": lots})
    errors.extend(checked["errors"])
    return {
        "valid": not errors,
        "errors": errors,
        "rows": lots,
        "portfolio": checked["portfolio"] if not errors else None,
    }


def build_advice(portfolio: dict[str, Any], fund_context: dict[str, Any], *, as_of: str) -> dict[str, Any]:
    """按基金代码聚合持仓并返回风险预算和人工复核建议。"""
    checked = validate_portfolio(portfolio)
    if not checked["valid"]:
        return {
            "state": "invalid_portfolio",
            "as_of": as_of,
            "positions": [],
            "risk": {"available": False, "reason": "持仓校验失败"},
            "warnings": [item["message"] for item in checked["errors"]],
            "reasons": ["持仓数据不可用"],
            "exact_amount_available": False,
        }
    normalized = checked["portfolio"]
    as_of_day = _parse_date(as_of)
    warnings: list[str] = []
    grouped: dict[str, dict[str, Any]] = {}
    total_cash = normalized["cash"]
    total_transit = 0.0
    for lot in normalized["lots"]:
        code = lot["code"]
        original_context = fund_context.get(code) if isinstance(fund_context, dict) else None
        original_context = original_context if isinstance(original_context, dict) else {}
        context = _apply_manual_metadata(original_context, lot, as_of_day)
        nav = _context_nav(context)
        nav_date = _parse_date(context.get("nav_date"))
        value, value_reason = _lot_value(lot, nav, nav_date, as_of_day)
        if value is None:
            warnings.append(f"{code}：{value_reason}")
            value = 0.0
        item = grouped.setdefault(
            code,
            {
                "code": code,
                "name": context.get("name") or code,
                "market_value": 0.0,
                "in_transit": 0.0,
                "lots": [],
                "context": context,
                "source_context": original_context,
                "value_reasons": [],
            },
        )
        item["market_value"] += value
        item["in_transit"] += lot["in_transit"]
        total_transit += lot["in_transit"]
        item["lots"].append(lot)
        if value_reason:
            item["value_reasons"].append(value_reason)
    total_assets = total_cash + total_transit + sum(item["market_value"] for item in grouped.values())
    if total_assets <= 0:
        return {
            "state": "unavailable",
            "as_of": as_of,
            "profile": normalized["profile"],
            "constraints": normalized["constraints"],
            "positions": [],
            "risk": {"available": False, "reason": "无法确定总资产"},
            "warnings": warnings + ["没有可核实的持仓市值"],
            "reasons": ["缺少真实净值或市值"],
            "exact_amount_available": False,
        }

    positions = []
    blocking_reasons = []
    for item in grouped.values():
        current_weight = item["market_value"] / total_assets
        baseline = _baseline_weight(item["lots"], current_weight)
        context = item["context"]
        industry = _industry(item["lots"], context)
        verified = _strategy_verified(context)
        source_state = str(context.get("state") or "unverified")
        reason = _position_reason(item, context, verified)
        if not verified:
            reason = "策略、概率或20日实盘观察未通过；仅提供当前持仓与基准风险参考"
        if item["value_reasons"]:
            reason = "; ".join(sorted(set(item["value_reasons"])))
            blocking_reasons.append(f"{item['code']}：{reason}")
        metadata_conflict = _metadata_conflict(item["lots"], item["source_context"], as_of_day)
        if metadata_conflict:
            blocking_reasons.append(f"{item['code']}：{metadata_conflict}")
            reason = metadata_conflict
        positions.append(
            {
                "code": item["code"],
                "name": item["name"],
                "industry": industry,
                "current_weight": _round(current_weight),
                "baseline_weight": _round(baseline),
                "target_min": None,
                "target_max": None,
                "target_weight": None,
                "market_value": _round(item["market_value"]),
                "in_transit": _round(item["in_transit"]),
                "lots_count": len(item["lots"]),
                "action": "observe",
                "reason": reason,
                "source_state": source_state,
                "signal_verified": verified,
            }
        )
    positions.sort(key=lambda item: item["code"])
    risk = _estimate_risk(positions, grouped, as_of=as_of_day)
    current_conflicts = _constraint_conflicts(positions, grouped, normalized["constraints"])
    projected, target_conflicts = _project_targets(
        positions, grouped, normalized, total_assets, as_of_day,
        blocked=bool(blocking_reasons),
    )
    conflicts = blocking_reasons + target_conflicts
    warnings.extend(current_conflicts + conflicts)
    if not risk["available"]:
        warnings.append(risk["reason"])
    advice = []
    if projected is not None:
        for position, target in zip(positions, projected):
            position["target_weight"] = _round(target)
            position["target_min"] = position["target_max"] = position["target_weight"]
            delta = target - position["current_weight"]
            if abs(delta) >= normalized["constraints"]["no_trade_band"] - 1e-8:
                action = "review_buy" if delta > 0 else "review_sell"
                position["action"] = action
                position["reason"] = "联合约束和策略信号均已核验；下单前仍须人工确认"
                advice.append({"code": position["code"], "action": action,
                               "current_weight": position["current_weight"],
                               "target_weight": position["target_weight"],
                               "delta_weight": _round(delta), "estimated_amount": None,
                               "reason": "单日联合风险预算内的人工复核动作"})
        target_risk = _estimate_risk(positions, grouped, weight_key="target_weight", as_of=as_of_day)
        if target_risk["available"]:
            risk["target_annualized_volatility"] = target_risk["annualized_volatility"]
    exact_amount_available, amount_reason = _exact_amount_gate(normalized, grouped, as_of_day, advice)
    if exact_amount_available:
        for action in advice:
            lots = grouped[action["code"]]["lots"]
            fee_key = "fee_buy" if action["action"] == "review_buy" else "fee_sell"
            rate = float(lots[0][fee_key])
            gross = abs(action["delta_weight"]) * total_assets
            action["estimated_amount"] = _round(gross)
            action["estimated_fee"] = _round(gross * rate)
            action["estimated_cash_impact"] = _round(
                -(gross * (1 + rate)) if action["action"] == "review_buy"
                else gross * (1 - rate)
            )
    if not exact_amount_available:
        warnings.append(amount_reason)
    state = "review_required" if warnings or not risk["available"] or not advice else "ready_for_review"
    return {
        "state": state,
        "as_of": as_of,
        "profile": normalized["profile"],
        "constraints": normalized["constraints"],
        "summary": {
            "total_assets": _round(total_assets),
            "cash": _round(total_cash),
            "in_transit": _round(total_transit),
            "fund_count": len(positions),
            "risk_budget": {
                "equity_cap": normalized["constraints"]["equity_cap"],
                "vol_target": normalized["constraints"]["vol_target"],
            },
        },
        "positions": positions,
        "risk": risk,
        "target_feasible": projected is not None,
        "advice": advice,
        "warnings": list(dict.fromkeys(warnings)),
        "reasons": list(dict.fromkeys(conflicts + ([risk["reason"]] if not risk["available"] else []))),
        "exact_amount_available": exact_amount_available,
        "exact_amount_reason": None if exact_amount_available else amount_reason,
    }


def _normalize_lot(value: Any, index: int, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    field = f"lots[{index}]"
    if not isinstance(value, dict):
        errors.append(_error(field, "必须是对象"))
        return None
    lot_id = str(value.get("id") or "").strip()
    if not lot_id:
        errors.append(_error(f"{field}.id", "不能为空"))
    code = str(value.get("code") or "").strip()
    if not code.isascii() or not code.isdigit() or len(code) != 6:
        errors.append(_error(f"{field}.code", "基金代码必须为六位数字字符串"))
    shares = _optional_number(value.get("shares"), f"{field}.shares", errors, minimum=0)
    market_value = _optional_number(value.get("market_value"), f"{field}.market_value", errors, minimum=0)
    if shares is None and market_value is None:
        errors.append(_error(field, "持有份额和持有市值不能同时为空"))
    valuation_date = _date_text(value.get("valuation_date"), f"{field}.valuation_date", errors, required=True)
    confirmed_date = _date_text(value.get("confirmed_date"), f"{field}.confirmed_date", errors, required=False)
    if valuation_date and confirmed_date and confirmed_date > valuation_date:
        errors.append(_error(field, "申购确认日期不能晚于估值日期"))
    baseline_weight = _optional_number(value.get("baseline_weight"), f"{field}.baseline_weight", errors, minimum=0, maximum=1)
    fee_buy = _optional_number(value.get("fee_buy"), f"{field}.fee_buy", errors, minimum=0, maximum=1)
    fee_sell = _optional_number(value.get("fee_sell"), f"{field}.fee_sell", errors, minimum=0, maximum=1)
    transit_input = value.get("in_transit")
    in_transit = _number(0 if transit_input is None or transit_input == "" else transit_input,
                         f"{field}.in_transit", errors, minimum=0)
    benchmark_symbol = value.get("benchmark_symbol")
    if benchmark_symbol is not None:
        benchmark_symbol = str(benchmark_symbol).strip() or None
    asset_class = _asset_class(value.get("asset_class"), f"{field}.asset_class", errors)
    metadata_verified = _optional_bool(value.get("metadata_verified"), f"{field}.metadata_verified", errors, default=False)
    effective_date = _date_text(value.get("effective_date"), f"{field}.effective_date", errors, required=False)
    if metadata_verified and (not benchmark_symbol or not asset_class or not effective_date):
        errors.append(_error(field, "已核实的手工元数据须同时提供基准代码、资产类别和生效日期"))
    industry = value.get("industry")
    if industry is not None:
        industry = str(industry).strip() or None
    return {
        "id": lot_id,
        "code": code,
        "shares": shares,
        "market_value": market_value,
        "valuation_date": valuation_date,
        "confirmed_date": confirmed_date,
        "fee_buy": fee_buy,
        "fee_sell": fee_sell,
        "in_transit": in_transit if in_transit is not None else 0.0,
        "baseline_weight": baseline_weight,
        "industry": industry,
        "benchmark_symbol": benchmark_symbol,
        "asset_class": asset_class,
        "metadata_verified": metadata_verified,
        "effective_date": effective_date,
    }


def _normalize_constraints(value: Any, profile: str, errors: list[dict[str, str]]) -> dict[str, float]:
    raw = value if isinstance(value, dict) else {}
    if value is not None and not isinstance(value, dict):
        errors.append(_error("constraints", "必须是对象"))
    for key in raw:
        if key not in _CONSTRAINT_KEYS:
            errors.append(_error(f"constraints.{key}", "不支持的约束字段"))
    defaults = {**_PROFILES[profile], **_DEFAULT_CONSTRAINTS}
    return {
        key: _number(raw.get(key, defaults[key]), f"constraints.{key}", errors, minimum=0, maximum=1)
        for key in sorted(_CONSTRAINT_KEYS)
    }


def _validate_constraint_relationships(constraints: dict[str, float | None], errors: list[dict[str, str]]) -> None:
    if any(value is None for value in constraints.values()):
        return
    if constraints["no_trade_band"] > constraints["daily_change_cap"]:
        errors.append(_error("constraints", "no_trade_band 不能大于 daily_change_cap"))
    if constraints["single_fund_cap"] > constraints["equity_cap"]:
        errors.append(_error("constraints", "single_fund_cap 不能大于 equity_cap"))
    if constraints["industry_cap"] > constraints["equity_cap"]:
        errors.append(_error("constraints", "industry_cap 不能大于 equity_cap"))


def _number(value: Any, field: str, errors: list[dict[str, str]], *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    return _optional_number(value, field, errors, minimum=minimum, maximum=maximum, required=True)


def _optional_number(value: Any, field: str, errors: list[dict[str, str]], *, minimum: float | None = None, maximum: float | None = None, required: bool = False) -> float | None:
    if value is None or value == "":
        if required:
            errors.append(_error(field, "不能为空"))
        return None
    if isinstance(value, bool):
        errors.append(_error(field, "必须是有限数字"))
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(_error(field, "必须是有限数字"))
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        errors.append(_error(field, "数值超出允许范围"))
        return None
    return number


def _date_text(value: Any, field: str, errors: list[dict[str, str]], *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            errors.append(_error(field, "不能为空，且必须为 YYYY-MM-DD"))
        return None
    parsed = _parse_date(value)
    if parsed is None:
        errors.append(_error(field, "必须为 YYYY-MM-DD"))
        return None
    return parsed.isoformat()


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _asset_class(value: Any, field: str, errors: list[dict[str, str]]) -> str | None:
    if value is None or value == "":
        return None
    mapping = {"权益": "equity", "equity": "equity", "债券": "bond", "bond": "bond", "货币": "money", "money": "money", "商品": "commodity", "commodity": "commodity", "其他": "other", "other": "other"}
    normalized = mapping.get(str(value).strip().lower())
    if normalized is None:
        errors.append(_error(field, "仅支持 权益、债券、货币、商品、其他"))
    return normalized


def _optional_bool(value: Any, field: str, errors: list[dict[str, str]], *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "是", "已核实"}:
        return True
    if normalized in {"false", "0", "否", "未核实"}:
        return False
    errors.append(_error(field, "必须是 true/false 或 是/否"))
    return default


def _context_nav(context: dict[str, Any]) -> float | None:
    value = context.get("nav")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _lot_value(lot: dict[str, Any], nav: float | None, nav_date: date | None, as_of: date | None) -> tuple[float | None, str | None]:
    if as_of is not None and _parse_date(lot["valuation_date"]) > as_of:
        return None, "持仓估值日期晚于决策日"
    if as_of is not None and lot.get("confirmed_date") and _parse_date(lot["confirmed_date"]) > as_of:
        return None, "申购份额确认日期晚于决策日，不能作为已到账持仓"
    if lot["shares"] is not None and nav is not None and nav_date is not None:
        if as_of is not None and nav_date > as_of:
            return None, "真实净值日期晚于估值日"
        if as_of is not None and (as_of - nav_date).days > 7:
            return lot["shares"] * nav, "真实净值距离决策日超过七个自然日，仅供参考估值"
        return lot["shares"] * nav, None
    if lot["market_value"] is not None:
        return lot["market_value"], "市值未以可核实的最新真实净值重算"
    return None, "缺少份额对应的真实净值和持有市值"


def _baseline_weight(lots: list[dict[str, Any]], current_weight: float) -> float:
    values = [lot["baseline_weight"] for lot in lots if lot["baseline_weight"] is not None]
    return sum(values) if values else current_weight


def _industry(lots: list[dict[str, Any]], context: dict[str, Any]) -> str | None:
    values = [str(lot["industry"]).strip() for lot in lots if lot.get("industry")]
    if values:
        return values[0]
    value = context.get("industry")
    return str(value).strip() if value else None


def _strategy_verified(context: dict[str, Any]) -> bool:
    strategy = context.get("strategy")
    forecast = context.get("forecast")
    if not isinstance(strategy, dict) or not isinstance(forecast, dict):
        return False
    signal = strategy.get("signal")
    validation = forecast.get("validation")
    published = forecast.get("published")
    if (not isinstance(signal, dict) or signal.get("eligible") is not True
            or signal.get("action") not in {"review_entry", "review_exit"}
            or not isinstance(validation, dict) or validation.get("publishable") is not True
            or not isinstance(published, dict) or forecast.get("state") != "published"
            or not isinstance(strategy.get("enabled"), bool) or strategy.get("enabled") is not True
            or strategy.get("status") != "passed" or context.get("state") != "ready"
            or context.get("observation_ready") is not True):
        return False
    event = validation.get("events", {}).get("20d_5pct", {})
    if not isinstance(event, dict) or event.get("passed") is not True:
        return False
    if "20d_5pct" not in published.get("probabilities", {}):
        return False
    if signal["action"] == "review_entry":
        interval = validation.get("intervals", {}).get("20d", {})
        if not isinstance(interval, dict) or interval.get("passed") is not True:
            return False
        if any(f"20d_q{quantile}" not in published.get("quantiles", {}) for quantile in (10, 50)):
            return False
        return strategy.get("entry_enabled") is True
    return strategy.get("exit_enabled") is True


def _metadata_conflict(lots: list[dict[str, Any]], context: dict[str, Any], as_of: date | None) -> str | None:
    if as_of is None:
        return "决策日期无效，无法核实基金映射"
    for field in ("benchmark_symbol", "asset_class", "industry"):
        values = {lot[field] for lot in lots if lot.get(field)}
        if len(values) > 1:
            return f"同一基金多个批次的{field}互相冲突"
    # 已核实手工映射可覆盖保守公共候选；只拦截批次之间的自相矛盾。
    return None


def _apply_manual_metadata(context: dict[str, Any], lot: dict[str, Any], as_of: date | None) -> dict[str, Any]:
    effective = _parse_date(lot.get("effective_date"))
    if not lot.get("metadata_verified") or effective is None or (as_of is not None and effective > as_of):
        return dict(context)
    updated = dict(context)
    updated["symbol"] = lot["benchmark_symbol"]
    updated["asset_class"] = lot["asset_class"]
    updated["metadata_verified"] = True
    updated["metadata_effective_date"] = lot["effective_date"]
    return updated


def _project_targets(
    positions: list[dict[str, Any]], grouped: dict[str, dict[str, Any]],
    portfolio: dict[str, Any], total_assets: float, as_of: date | None, *, blocked: bool,
) -> tuple[list[float] | None, list[str]]:
    """一次求解并逐项复核全部硬约束；不可行时所有目标均为空。"""
    constraints = portfolio["constraints"]
    if blocked or as_of is None:
        return None, ["持仓估值或基金元数据冲突，联合目标不可执行"]
    active = [index for index, item in enumerate(positions) if item["signal_verified"]]
    if not active:
        return None, ["没有经过概率、策略和实盘观察共同验证的调仓信号"]
    for position in positions:
        context = grouped[position["code"]]["context"]
        if context.get("asset_class") not in {"equity", "bond", "money", "commodity", "other"}:
            return None, [f"{position['code']} 资产类别未核实，无法检查权益上限"]
        if context.get("asset_class") == "equity" and not position.get("industry"):
            return None, [f"{position['code']} 行业未核实，无法检查行业上限"]
    current = np.asarray([item["current_weight"] for item in positions], dtype=float)
    n = len(current)
    band = max(0.05, constraints["no_trade_band"])
    desired = current.copy()
    bounds: list[tuple[float, float | None]] = []
    fees = np.zeros(n)
    for index, position in enumerate(positions):
        code = position["code"]
        if index not in active:
            bounds.append((float(current[index]), float(current[index])))
            continue
        lots = grouped[code]["lots"]
        if any(lot["confirmed_date"] is None or _parse_date(lot["confirmed_date"]) > as_of
               or lot["fee_buy"] is None or lot["fee_sell"] is None for lot in lots):
            return None, [f"{code} 确认日或逐批次申赎费率未核实，不能给出可执行目标"]
        fees[index] = max(float(lot["fee_buy"]) for lot in lots)
        signal = grouped[code]["context"]["strategy"]["signal"]["action"]
        if signal == "review_entry":
            lower, upper = float(current[index] + band), constraints["single_fund_cap"]
            desired[index] = lower
        else:
            lower, upper = 0.0, float(current[index] - band)
            desired[index] = upper
        if lower > upper + 1e-9 or upper < 0:
            return None, [f"{code} 信号要求至少{band:.1%}调整，但单只上限或现有仓位不允许"]
        bounds.append((lower, upper))
    covariance, risk_error = _covariance_matrix(positions, grouped, as_of, active)
    if covariance is None:
        return None, [risk_error]
    equity = [index for index, item in enumerate(positions)
              if grouped[item["code"]]["context"]["asset_class"] == "equity"]
    industries: defaultdict[str, list[int]] = defaultdict(list)
    for index, position in enumerate(positions):
        if position["industry"]:
            industries[position["industry"]].append(index)
    try:
        from scipy.optimize import minimize
    except ImportError:
        return None, ["scipy 求解器缺失，无法核验联合可行目标"]
    auxiliary_bounds = [(0.0, None)] * (2 * n)
    initial = np.concatenate([desired, np.abs(desired - current), np.maximum(desired - current, 0)])
    requirements = [
        {"type": "ineq", "fun": lambda vector: vector[n:2*n] - (vector[:n] - current)},
        {"type": "ineq", "fun": lambda vector: vector[n:2*n] + (vector[:n] - current)},
        {"type": "ineq", "fun": lambda vector: vector[2*n:3*n] - (vector[:n] - current)},
        {"type": "ineq", "fun": lambda vector: constraints["daily_change_cap"] - np.sum(vector[n:2*n])},
        {"type": "ineq", "fun": lambda vector: portfolio["cash"] - total_assets *
         np.dot(vector[2*n:3*n], 1.0 + fees)},
        {"type": "ineq", "fun": lambda vector: 1.0 - np.sum(vector[:n])},
        {"type": "ineq", "fun": lambda vector: constraints["equity_cap"] - np.sum(vector[equity])},
        {"type": "ineq", "fun": lambda vector: constraints["vol_target"] ** 2 -
         252.0 * float(vector[:n] @ covariance @ vector[:n])},
    ]
    for indexes in industries.values():
        requirements.append({"type": "ineq", "fun": lambda vector, group=indexes:
                             constraints["industry_cap"] - np.sum(vector[group])})
    solution = minimize(
        lambda vector: float(np.sum((vector[:n] - desired) ** 2) +
                             1e-6 * np.sum(vector[n:3*n])),
        initial, bounds=bounds + auxiliary_bounds, constraints=requirements,
        method="SLSQP", options={"maxiter": 500, "ftol": 1e-12},
    )
    if not solution.success or not np.all(np.isfinite(solution.x)):
        return None, [f"权益、单只、行业、现金、单日总调仓与波动目标无法同时满足：{solution.message}"]
    target = np.asarray(solution.x[:n], dtype=float)
    delta = target - current
    tolerance = 1e-7
    violations = []
    if np.any(target < -tolerance) or np.any(target > constraints["single_fund_cap"] + tolerance):
        violations.append("单只基金上限")
    if target.sum() > 1.0 + tolerance:
        violations.append("总权重不超过100%")
    if target[equity].sum() > constraints["equity_cap"] + tolerance:
        violations.append("权益上限")
    if any(target[indexes].sum() > constraints["industry_cap"] + tolerance
           for indexes in industries.values()):
        violations.append("行业上限")
    if np.abs(delta).sum() > constraints["daily_change_cap"] + tolerance:
        violations.append("单日买卖绝对权重合计")
    if total_assets * np.dot(np.maximum(delta, 0), 1 + fees) > portfolio["cash"] + tolerance:
        violations.append("只使用已到账现金支付买入及申购费")
    if 252.0 * float(target @ covariance @ target) > constraints["vol_target"] ** 2 + tolerance * 1e-4:
        violations.append("组合波动目标")
    if any((abs(delta[index]) < band - tolerance or
            (grouped[positions[index]["code"]]["context"]["strategy"]["signal"]["action"] == "review_entry"
             and delta[index] < 0) or
            (grouped[positions[index]["code"]]["context"]["strategy"]["signal"]["action"] == "review_exit"
             and delta[index] > 0)) for index in active):
        violations.append("信号方向和5%免交易带")
    if any(abs(delta[index]) > tolerance for index in range(n) if index not in active):
        violations.append("未验证基金必须保持当前仓位")
    if violations:
        return None, ["联合优化数值结果未通过硬约束复核：" + "、".join(violations)]
    return target.tolist(), []


def _position_reason(item: dict[str, Any], context: dict[str, Any], verified: bool) -> str:
    if not context:
        return "缺少基金上下文，等待核实标的与净值"
    if not context.get("symbol"):
        return "基准未核实，无法假定为 A 股"
    return "策略验证通过，仍需人工确认" if verified else "仅目标风险参考范围"


def _constraint_conflicts(
    positions: list[dict[str, Any]], grouped: dict[str, dict[str, Any]], constraints: dict[str, float]
) -> list[str]:
    conflicts: list[str] = []
    industry_weights: defaultdict[str, float] = defaultdict(float)
    equity_weight = 0.0
    unknown_asset_class = False
    for position in positions:
        weight = position["current_weight"]
        asset_class = grouped[position["code"]]["context"].get("asset_class")
        if asset_class == "equity":
            equity_weight += weight
        elif asset_class is None:
            unknown_asset_class = True
        if weight > constraints["single_fund_cap"]:
            conflicts.append(f"{position['code']} 当前权重超过 single_fund_cap")
        if position.get("industry"):
            industry_weights[position["industry"]] += weight
    if equity_weight > constraints["equity_cap"]:
        conflicts.append("已核实为权益资产的持仓权重超过 equity_cap")
    if unknown_asset_class:
        conflicts.append("部分基金资产类别未核实；未将其假定为 A 股，也无法完整检查 equity_cap")
    for industry, weight in industry_weights.items():
        if weight > constraints["industry_cap"]:
            conflicts.append(f"行业 {industry} 当前权重超过 industry_cap")
    return conflicts


def _covariance_matrix(
    positions: list[dict[str, Any]], grouped: dict[str, dict[str, Any]],
    as_of: date | None, extra_indices: list[int],
) -> tuple[np.ndarray | None, str]:
    included = [index for index, position in enumerate(positions)
                if position["current_weight"] > 0 or index in extra_indices]
    if not included:
        return None, "没有可用于风险估计的基金市值"
    series: dict[str, dict[str, tuple[float, Any, bool]]] = {}
    ordered_days: dict[str, list[str]] = {}
    for index in included:
        code = positions[index]["code"]
        rows = grouped[code]["context"].get("returns")
        if not isinstance(rows, list):
            return None, f"{code} 缺少真实、连续的基金日收益率"
        parsed: dict[str, tuple[float, Any, bool]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = _parse_date(row.get("trade_date"))
            if day is None or as_of is None or day > as_of:
                continue
            try:
                value = float(row.get("return"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or not -1 < value < 1:
                continue
            key = day.isoformat()
            if key in parsed:
                return None, f"{code} 同日收益率重复，无法核验风险"
            parsed[key] = (value, row.get("segment"), bool(row.get("gap_before")))
        series[code] = parsed
        ordered_days[code] = sorted(parsed)
    common = set.intersection(*(set(rows) for rows in series.values())) if series else set()
    if len(common) < 252:
        return None, "共同真实基金日收益率不足252个交易日"
    days = sorted(common)[-252:]
    if as_of is not None and (as_of - date.fromisoformat(days[-1])).days > 7:
        return None, "共同基金收益率最近日期距决策日超过7个自然日"
    for code, entries in series.items():
        positions_in_source = [ordered_days[code].index(day) for day in days]
        if positions_in_source[-1] - positions_in_source[0] != 251:
            return None, f"{code} 最近252日共同窗口中存在不同步缺口"
        values = [entries[day] for day in days]
        segments = {item[1] for item in values if item[1] is not None}
        if (any(item[2] for item in values) or len(segments) != 1
                or any(item[1] is None or item[1] == "" for item in values)):
            return None, f"{code} 最近252日跨收益率缺口或复权分段"
    matrix = np.asarray([[series[positions[index]["code"]][day][0] for index in included]
                         for day in days], dtype=float)
    try:
        from sklearn.covariance import LedoitWolf
        submatrix = LedoitWolf().fit(matrix).covariance_
    except (ImportError, ValueError, FloatingPointError) as error:
        return None, f"LedoitWolf真实收益率估计失败：{error}"
    covariance = np.zeros((len(positions), len(positions)), dtype=float)
    covariance[np.ix_(included, included)] = np.atleast_2d(submatrix)
    return covariance, ""


def _estimate_risk(
    positions: list[dict[str, Any]], grouped: dict[str, dict[str, Any]], *,
    weight_key: str = "current_weight", as_of: date | None = None,
) -> dict[str, Any]:
    included = [index for index, position in enumerate(positions)
                if position.get(weight_key) is not None and position[weight_key] > 0]
    if not included:
        return {"available": False, "reason": "没有可用于风险估计的基金市值"}
    covariance, error = _covariance_matrix(positions, grouped, as_of, included)
    if covariance is None:
        return {"available": False, "reason": error}
    weights = np.asarray([position.get(weight_key) or 0.0 for position in positions])
    variance = float(weights @ covariance @ weights)
    if not math.isfinite(variance) or variance < 0:
        return {"available": False, "reason": "协方差矩阵无法给出有效风险"}
    return {"available": True, "method": "LedoitWolf", "observations": 252,
            "assets": [positions[index]["code"] for index in included],
            "annualized_volatility": _round(math.sqrt(variance * 252))}


def _exact_amount_gate(portfolio: dict[str, Any], grouped: dict[str, dict[str, Any]],
                       as_of: date | None, advice: list[dict[str, Any]]) -> tuple[bool, str]:
    if not advice:
        return False, "没有已验证且联合可行的调仓动作"
    if as_of is None:
        return False, "估值日期无效，不能计算精确金额"
    for action in advice:
        code = action["code"]
        item = grouped[code]
        for lot in item["lots"]:
            if lot["shares"] is None:
                return False, f"{code} 存在未提供持有份额的批次，不能计算精确金额"
            if lot["fee_buy"] is None or lot["fee_sell"] is None:
                return False, f"{code} 逐批次费用未核实，不能计算精确金额"
            if lot["confirmed_date"] is None or _parse_date(lot["confirmed_date"]) > as_of:
                return False, f"{code} 份额确认日尚未核实，不能计算精确金额"
            if _parse_date(lot["valuation_date"]) > as_of:
                return False, f"{code} 估值日期晚于决策日"
        fee_key = "fee_buy" if action["action"] == "review_buy" else "fee_sell"
        if len({lot[fee_key] for lot in item["lots"]}) != 1:
            return False, f"{code} 不同批次{fee_key}费率不一致，缺少卖出批次分配依据"
        context = item["context"]
        if not context.get("fees_verified") or not context.get("calendar_verified"):
            return False, f"{code} 的费用或交易日历未核实，不能计算精确金额"
        execution=context.get("execution",{})
        if not execution.get("confirmation_days"):
            return False, f"{code} 仅核实可提交日期，确认周期及到账时间未核实；仅给仓位区间"
        nav_day = _parse_date(context.get("nav_date"))
        if nav_day is None or nav_day > as_of or (as_of-nav_day).days > 7:
            return False, f"{code} 缺少截至决策日可用的真实净值"
    return True, ""


def _round(value: float) -> float:
    return round(float(value), 10)


def _error(field: str, message: str) -> dict[str, str]:
    return {"field": field, "message": message}
