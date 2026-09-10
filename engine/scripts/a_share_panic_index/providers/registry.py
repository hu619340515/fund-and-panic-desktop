"""主备链、重试、熔断和离线夹具。"""

from __future__ import annotations

import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..features.derivatives import contract_expiry
from .base import (
    ProviderError,
    ProviderRetryableError,
    ProviderTimeout,
    ProviderUnavailable,
    run_with_hard_timeout,
)
from .live import fetch_live


def provider_worker(provider: str, semantic_type: str, context: dict[str, Any]):
    return fetch_live(provider, semantic_type, context)


class ProviderManager:
    def __init__(self, settings, database, logger):
        self.settings = settings
        self.database = database
        self.logger = logger
        self.network = settings.section("network")
        self.priorities = settings.section("providers")
        self.health = {
            (item["provider"], item["semantic_type"]): item
            for item in database.provider_status()
        }
        self.last_events: list[dict[str, Any]] = []
        self.last_disagreements: list[dict[str, Any]] = []
        self.refresh_started: float | None = None

    def collect(
        self,
        now: datetime,
        trade_date,
        fixture: str | Path | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        self.last_events = []
        self.last_disagreements = []
        if fixture:
            return self._load_fixture(Path(fixture), now), []
        started = time.monotonic()
        self.refresh_started = started
        context = {
            "now": now.isoformat(),
            "trade_date": trade_date.isoformat(),
            "symbol": self.settings.get("market.index_symbol"),
            "proxy_symbols": self.settings.get("market.etf_symbols"),
            "proxy_history_natural_days": self.settings.get(
                "reference.proxy_history_natural_days"
            ),
            "timeout": float(self.network["provider_timeout_seconds"]),
        }
        results: dict[str, dict[str, Any]] = {}
        events: list[dict[str, Any]] = []
        for semantic in ("index", "breadth", "limits", "futures"):
            try:
                result, source_events = self._fetch_chain(
                    semantic, context, started, allow_missing=False
                )
            except ProviderUnavailable:
                if semantic != "limits" or "breadth" not in results:
                    raise
                result, source_events = self._estimate_limits(
                    results["breadth"], context
                )
                self.last_events.extend(source_events)
            results[semantic] = result
            events.extend(source_events)
        qvix, qvix_events = self._fetch_chain(
            "qvix", context, started, allow_missing=True
        )
        if qvix is not None:
            results["qvix"] = qvix
        events.extend(qvix_events)
        baseline, baseline_events = self._fetch_specific(
            "sina", "daily_baseline", context, started
        )
        results["daily_baseline"] = baseline
        events.extend(baseline_events)
        return results, events

    def collect_proxy_curve(
        self,
        now: datetime,
        trade_date,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        started = self.refresh_started or time.monotonic()
        context = {
            "now": now.isoformat(),
            "trade_date": trade_date.isoformat(),
            "symbol": self.settings.get("market.index_symbol"),
            "proxy_symbols": self.settings.get("market.etf_symbols"),
            "proxy_history_natural_days": self.settings.get(
                "reference.proxy_history_natural_days"
            ),
            "timeout": float(self.network["provider_timeout_seconds"]),
        }
        return self._fetch_chain(
            "proxy_curve", context, started, allow_missing=True
        )

    @staticmethod
    def _estimate_limits(
        breadth_result: dict[str, Any], context: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        data = breadth_result.get("data", {})
        values = [float(item) for item in data.get("change_percent_values", [])]
        up = sum(value >= 0.095 for value in values)
        down = sum(value <= -0.095 for value in values)
        if up == 0 and down == 0:
            raise ProviderUnavailable("无法基于全市场涨跌幅估算涨跌停")
        now = context["now"]
        result = {
            "provider": "breadth_estimate",
            "semantic_type": "limits",
            "data": {"limit_up": int(up), "limit_down": int(down)},
            "source_timestamp": now,
            "requested_at": now,
            "received_at": now,
            "provisional": True,
            "quality_flags": ["limits_estimated_from_breadth"],
            "latency_ms": 0.0,
        }
        event = {
            "provider": "breadth_estimate",
            "semantic_type": "limits",
            "success": True,
            "latency_ms": 0.0,
        }
        return result, [event]

    def _fetch_chain(
        self,
        semantic_type: str,
        context: dict[str, Any],
        started: float,
        allow_missing: bool,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        providers = list(self.priorities.get(semantic_type, []))
        if semantic_type == "limits":
            providers = [item for item in providers if item != "breadth_estimate"]
        errors: list[str] = []
        for provider in providers:
            if self._circuit_open(provider, semantic_type):
                message = "熔断冷却中"
                errors.append(f"{provider}: {message}")
                event = {
                    "provider": provider,
                    "semantic_type": semantic_type,
                    "success": False,
                    "error": message,
                }
                events.append(event)
                self.last_events.append(event)
                continue
            try:
                result, source_events = self._fetch_specific(
                    provider,
                    semantic_type,
                    context,
                    started,
                    max_attempts=1 if semantic_type == "proxy_curve" else None,
                )
                events.extend(source_events)
                self._compare_with_backup(
                    result,
                    providers[providers.index(provider) + 1 :],
                    semantic_type,
                    context,
                    started,
                    events,
                )
                return result, events
            except ProviderError as error:
                errors.append(f"{provider}: {error}")
                self.logger.warning(
                    "数据源失败 provider=%s semantic=%s error=%s",
                    provider,
                    semantic_type,
                    error,
                )
        if allow_missing:
            return None, events
        raise ProviderUnavailable(
            f"{semantic_type}全部数据源失败: " + "; ".join(errors)
        )

    def _fetch_specific(
        self,
        provider: str,
        semantic_type: str,
        context: dict[str, Any],
        started: float,
        max_attempts: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        max_retries = int(max_attempts or self.network["max_retries"])
        base_delay = float(self.network["retry_delay_seconds"])
        provider_timeout = float(self.network["provider_timeout_seconds"])
        total_timeout = float(self.network["total_refresh_timeout_seconds"])
        last_error: Exception | None = None
        for attempt in range(max_retries):
            remaining = total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ProviderTimeout(f"整轮刷新超过 {total_timeout:g} 秒")
            attempt_timeout = min(provider_timeout, remaining)
            worker_context = dict(context)
            worker_context["timeout"] = max(0.1, attempt_timeout)
            try:
                result = run_with_hard_timeout(
                    provider_worker,
                    (provider, semantic_type, worker_context),
                    attempt_timeout,
                )
                event = {
                    "provider": provider,
                    "semantic_type": semantic_type,
                    "success": True,
                    "latency_ms": result.get("latency_ms", 0),
                }
                events.append(event)
                self.last_events.append(event)
                return result, events
            except ProviderRetryableError as error:
                last_error = error
                event = {
                    "provider": provider,
                    "semantic_type": semantic_type,
                    "success": False,
                    "error": str(error),
                    "retryable": True,
                }
                events.append(event)
                self.last_events.append(event)
                if attempt + 1 < max_retries:
                    remaining = total_timeout - (time.monotonic() - started)
                    delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
                    if delay >= remaining:
                        raise ProviderTimeout(
                            f"整轮刷新剩余时间不足，无法继续重试 {provider}/{semantic_type}"
                        ) from error
                    time.sleep(delay)
            except ProviderError as error:
                event = {
                    "provider": provider,
                    "semantic_type": semantic_type,
                    "success": False,
                    "error": str(error),
                    "retryable": False,
                }
                events.append(event)
                self.last_events.append(event)
                raise
        raise ProviderError(str(last_error or "未知数据源错误"))

    def _compare_with_backup(
        self,
        primary: dict[str, Any],
        providers: list[str],
        semantic_type: str,
        context: dict[str, Any],
        started: float,
        events: list[dict[str, Any]],
    ) -> None:
        if semantic_type not in {"index", "futures", "qvix"}:
            return
        for provider in providers:
            if self._circuit_open(provider, semantic_type):
                continue
            try:
                secondary, source_events = self._fetch_specific(
                    provider,
                    semantic_type,
                    context,
                    started,
                    max_attempts=1,
                )
            except ProviderError:
                return
            events.extend(source_events)
            comparison = self._compare_values(
                primary,
                secondary,
                semantic_type,
                context.get("trade_date"),
            )
            if comparison is None:
                return
            primary["comparison"] = comparison
            self.last_disagreements.append(comparison)
            if comparison["exceeds_tolerance"]:
                flag = f"cross_source_disagreement:{provider}"
                primary.setdefault("quality_flags", []).append(flag)
                primary["provisional"] = True
            return

    def _compare_values(
        self,
        primary: dict[str, Any],
        secondary: dict[str, Any],
        semantic_type: str,
        trade_date: str | date | None = None,
    ) -> dict[str, Any] | None:
        compared_symbol = None
        if semantic_type == "futures":
            primary_contracts = {
                str(item.get("symbol")): item
                for item in primary.get("data", {}).get("contracts", [])
                if item.get("symbol")
            }
            secondary_contracts = {
                str(item.get("symbol")): item
                for item in secondary.get("data", {}).get("contracts", [])
                if item.get("symbol")
            }
            common = sorted(set(primary_contracts) & set(secondary_contracts))
            if trade_date:
                target = (
                    trade_date
                    if isinstance(trade_date, date)
                    else date.fromisoformat(str(trade_date))
                )
                minimum_days = int(
                    self.settings.get("futures.minimum_days_to_expiry")
                )
                common = [
                    symbol
                    for symbol in common
                    if (contract_expiry(symbol) - target).days >= minimum_days
                ]
            if not common:
                return None
            compared_symbol = min(common, key=contract_expiry)
            first = self._contract_value(primary_contracts[compared_symbol])
            second = self._contract_value(secondary_contracts[compared_symbol])
        else:
            first = self._semantic_value(primary, semantic_type)
            second = self._semantic_value(secondary, semantic_type)
        if first is None or second is None:
            return None
        difference = abs(first - second) / max(abs(first), abs(second), 1e-9)
        tolerance = {
            "index": float(self.settings.get("quality.index_cross_source_tolerance")),
            "futures": float(self.settings.get("quality.futures_cross_source_tolerance")),
            "qvix": float(self.settings.get("quality.qvix_cross_source_tolerance")),
        }[semantic_type]
        return {
            "semantic_type": semantic_type,
            "primary_provider": primary.get("provider"),
            "secondary_provider": secondary.get("provider"),
            "primary_value": first,
            "secondary_value": second,
            "difference_ratio": difference,
            "tolerance": tolerance,
            "exceeds_tolerance": difference > tolerance,
            "compared_symbol": compared_symbol,
        }

    @staticmethod
    def _semantic_value(result: dict[str, Any], semantic_type: str) -> float | None:
        data = result.get("data", {})
        try:
            if semantic_type == "index":
                return float(data["last"])
            if semantic_type == "qvix":
                return float(data["value"])
            if semantic_type == "futures":
                contracts = data.get("contracts", [])
                if not contracts:
                    return None
                return ProviderManager._contract_value(contracts[0])
        except (KeyError, TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _contract_value(contract: dict[str, Any]) -> float | None:
        try:
            bid = contract.get("bid")
            ask = contract.get("ask")
            if bid is not None and ask is not None:
                return (float(bid) + float(ask)) / 2.0
            return float(contract.get("last", contract.get("price")))
        except (TypeError, ValueError):
            return None

    def _circuit_open(self, provider: str, semantic_type: str) -> bool:
        state = self.health.get((provider, semantic_type))
        if not state:
            return False
        threshold = int(self.network["circuit_failure_threshold"])
        if int(state.get("consecutive_failures", 0)) < threshold:
            return False
        last_failure = state.get("last_failure_at")
        if not last_failure:
            return False
        failed_at = datetime.fromisoformat(last_failure)
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        cooldown = timedelta(seconds=float(self.network["circuit_cooldown_seconds"]))
        return datetime.now(ZoneInfo("Asia/Shanghai")) < failed_at + cooldown

    def _load_fixture(
        self, fixture: Path, now: datetime
    ) -> dict[str, dict[str, Any]]:
        path = fixture
        if path.is_dir():
            candidates = [
                path / f"{now.strftime('%H%M')}.json",
                path / "snapshot.json",
            ]
            path = next((item for item in candidates if item.exists()), candidates[-1])
        if not path.exists():
            raise FileNotFoundError(f"实时夹具不存在: {path}")
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        providers = payload.get("providers", payload)
        required = {"index", "breadth", "limits", "futures", "daily_baseline"}
        missing = sorted(required - set(providers))
        if missing:
            raise ValueError("实时夹具缺少语义: " + ", ".join(missing))
        normalized = {}
        for semantic, item in providers.items():
            result = dict(item)
            result.setdefault("provider", f"fixture_{semantic}")
            result.setdefault("semantic_type", semantic)
            result.setdefault("source_timestamp", now.isoformat())
            result.setdefault("requested_at", now.isoformat())
            result.setdefault("received_at", now.isoformat())
            result.setdefault("provisional", False)
            result.setdefault("quality_flags", [])
            result.setdefault("latency_ms", 0.0)
            if "data" not in result:
                data_keys = {
                    key: value
                    for key, value in result.items()
                    if key not in {
                        "provider", "semantic_type", "source_timestamp",
                        "requested_at", "received_at", "provisional",
                        "quality_flags", "latency_ms",
                    }
                }
                result["data"] = data_keys
            normalized[semantic] = result
        return normalized
