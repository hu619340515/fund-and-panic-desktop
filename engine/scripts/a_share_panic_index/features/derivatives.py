"""明确 IF 合约选择和基差特征。"""

from __future__ import annotations

import re
from calendar import monthcalendar
from datetime import date
from math import isfinite
from typing import Any


CONTRACT_PATTERN = re.compile(r"^IF(\d{2})(\d{2})$")


def contract_expiry(symbol: str) -> date:
    match = CONTRACT_PATTERN.match(symbol.upper())
    if not match:
        raise ValueError(f"不是明确IF合约: {symbol}")
    year = 2000 + int(match.group(1))
    month = int(match.group(2))
    fridays = [week[4] for week in monthcalendar(year, month) if week[4]]
    return date(year, month, fridays[2])


def mid_or_last(contract: dict[str, Any]) -> float:
    bid = _positive(contract.get("bid"))
    ask = _positive(contract.get("ask"))
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    last = _positive(contract.get("last"))
    if last is None:
        raise ValueError(f"IF合约无有效报价: {contract.get('symbol')}")
    return last


def select_if_contracts(
    contracts: list[dict[str, Any]],
    trade_date: date,
    minimum_days_to_expiry: int = 5,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    valid: list[dict[str, Any]] = []
    for item in contracts:
        symbol = str(item.get("symbol", "")).upper()
        if not CONTRACT_PATTERN.match(symbol):
            continue
        expiry = item.get("expiry")
        if isinstance(expiry, str):
            expiry = date.fromisoformat(expiry)
        expiry = expiry or contract_expiry(symbol)
        days = (expiry - trade_date).days
        if days < minimum_days_to_expiry:
            continue
        try:
            price = mid_or_last(item)
        except ValueError:
            continue
        normalized = dict(item)
        normalized.update({"symbol": symbol, "expiry": expiry, "price": price})
        valid.append(normalized)
    valid.sort(key=lambda item: (item["expiry"], item["symbol"]))
    if not valid:
        raise ValueError("没有满足换月规则的明确IF合约")
    return valid[0], valid[1] if len(valid) > 1 else None


def annualized_basis(
    spot: float,
    futures_price: float,
    days_to_expiry: int,
    annualization_days: int = 365,
) -> float:
    if spot <= 0 or futures_price <= 0 or days_to_expiry <= 0:
        raise ValueError("基差输入必须为正数")
    return ((float(spot) / float(futures_price)) - 1.0) * annualization_days / days_to_expiry


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None
