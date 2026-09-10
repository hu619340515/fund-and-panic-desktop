"""盘中采集、聚合、评分和原子落库。"""

from __future__ import annotations

from datetime import date, datetime, time
from math import isfinite
from typing import Any

from ..calendar import TradingCalendar
from ..features.derivatives import select_if_contracts
from ..features.liquidity import (
    blended_cumulative_share,
    bootstrap_cumulative_share,
)
from ..features.realtime import build_realtime_feature_values
from ..models import AggregateSnapshot, RealtimeResult
from ..providers import ProviderError, ProviderManager
from ..scoring import (
    blend_with_history,
    classify_level,
    generalized_mean,
    reference_state,
    score_from_anchors,
    smooth_display,
    weighted_available_score,
)


class PipelineError(RuntimeError):
    pass


class StaleDataError(PipelineError):
    def __init__(self, message: str, stale_sources: list[str] | None = None):
        super().__init__(message)
        self.stale_sources = stale_sources or []


class IncompleteDataError(PipelineError):
    pass


class RealtimePipeline:
    def __init__(self, settings, database, logger):
        self.settings = settings
        self.database = database
        self.logger = logger
        market = settings.section("market")
        self.calendar = TradingCalendar(
            market.get("calendar", "XSHG"), market.get("timezone", "Asia/Shanghai")
        )
        self.providers = ProviderManager(settings, database, logger)

    def run(
        self,
        now: datetime,
        requested_date: date | None = None,
        fixture: str | None = None,
        persist: bool = True,
    ) -> tuple[RealtimeResult | None, dict[str, Any]]:
        context = self.calendar.context(now, requested_date)
        self.logger.info(
            "realtime开始 requested=%s expected=%s phase=%s fixture=%s",
            context.requested_date,
            context.expected_trade_date,
            context.phase,
            bool(fixture),
        )
        if not context.is_trading_day:
            return None, {
                "context": context.to_dict(),
                "status": "skipped_non_trading_day",
                "latest_realtime": self.database.latest_realtime(),
                "provider_events": [],
            }
        if context.phase == "lunch_break":
            return None, {
                "context": context.to_dict(),
                "status": "lunch_break_frozen",
                "latest_realtime": self.database.latest_realtime(),
                "provider_events": [],
            }
        if context.session_minute is None:
            return None, {
                "context": context.to_dict(),
                "status": "market_not_ready",
                "latest_realtime": self.database.latest_realtime(),
                "provider_events": [],
            }
        try:
            results, events = self.providers.collect(
                context.now, context.expected_trade_date, fixture
            )
            if not fixture and self.database.reference_curve(
                context.bucket_5m or 0, "proxy_market"
            ) is None:
                proxy_result, proxy_events = self.providers.collect_proxy_curve(
                    context.now, context.expected_trade_date
                )
                events.extend(proxy_events)
                if proxy_result:
                    proxy_data = proxy_result["data"]
                    curve_values = {
                        int(bucket): float(value)
                        for bucket, value in proxy_data["curve"].items()
                    }
                    self.database.upsert_reference_curve(
                        "proxy_market",
                        curve_values,
                        int(proxy_data["sample_days"]),
                        str(proxy_result["provider"]),
                    )
                    results["proxy_curve"] = proxy_result
            freshness_clock = self._freshness_clock(context)
            self._drop_stale_optional_sources(results, freshness_clock)
            aggregate = self._build_aggregate(context, results)
            stale = self._stale_sources(results, freshness_clock)
            if stale:
                raise StaleDataError("核心数据源已过期: " + ", ".join(stale), stale)
            core_timestamps = {
                name: self._effective_source_timestamp(
                    str(results[name]["source_timestamp"]), freshness_clock
                )
                for name in ("index", "breadth", "limits", "futures")
            }
            source_skew = self._source_skew(core_timestamps)
            if source_skew > float(
                self.settings.get("freshness.max_cross_source_skew_seconds")
            ):
                raise StaleDataError(
                    f"核心来源时间偏差过大: {source_skew:.0f} 秒",
                    ["cross_source_skew"],
                )
            history = self.database.current_day_aggregates(context.expected_trade_date)
            previous_bucket = self.database.previous_bucket_aggregate(
                context.expected_trade_date, aggregate.bucket_5m
            )
            result, history_days, blend_weight = self._score(
                aggregate, history, previous_bucket, context.now
            )
        except (ProviderError, IncompleteDataError, StaleDataError):
            self.database.record_provider_events(self.providers.last_events)
            raise
        if persist:
            self.database.write_realtime(
                aggregate, result, history_days, blend_weight, events
            )
        self.logger.info(
            "realtime完成 trade_date=%s timestamp=%s raw=%.4f display=%.4f quality=%s",
            result.trade_date,
            result.timestamp,
            result.realtime_panic_index_raw,
            result.realtime_panic_index,
            result.quality_status,
        )
        return result, {
            "context": context.to_dict(),
            "aggregate": aggregate.to_dict(),
            "provider_events": events,
            "status": "success",
        }

    def _build_aggregate(
        self, context, results: dict[str, dict[str, Any]]
    ) -> AggregateSnapshot:
        index = results["index"]["data"]
        breadth = results["breadth"]["data"]
        limits = results["limits"]["data"]
        futures = results["futures"]["data"]
        baseline = results["daily_baseline"]["data"]
        try:
            front, next_contract = select_if_contracts(
                futures["contracts"],
                context.expected_trade_date,
                int(self.settings.get("futures.minimum_days_to_expiry")),
            )
        except (KeyError, ValueError) as error:
            raise IncompleteDataError(f"IF合约选择失败: {error}") from error
        proxy_curve = self.database.reference_curve(
            context.bucket_5m or 0, "proxy_market"
        )
        self_curve = self.database.reference_curve(
            context.bucket_5m or 0, "self_market"
        )
        history_days = self._same_bucket_days(context.bucket_5m or 0, context.expected_trade_date)
        if proxy_curve:
            proxy_share = float(proxy_curve["cumulative_share"])
            proxy_mode = "real_proxy_curve"
        else:
            proxy_share = bootstrap_cumulative_share(context.bucket_5m or 0)
            proxy_mode = "structural_bootstrap"
        self_share = float(self_curve["cumulative_share"]) if self_curve else None
        expected_share, curve_mode, curve_weight = blended_cumulative_share(
            proxy_share, self_share, history_days
        )
        market_amount = _required_number(breadth.get("market_amount"), "全市场成交额")
        projected = market_amount / max(expected_share, 1e-6)
        current_day = self.database.current_day_aggregates(context.expected_trade_date)
        previous_amount = float(current_day[-1]["market_amount"]) if current_day else None
        incremental = market_amount - previous_amount if previous_amount is not None else market_amount
        if incremental < 0:
            raise IncompleteDataError("全市场累计成交额出现倒退")
        qvix = results.get("qvix")
        qvix_data = qvix["data"] if qvix else {}
        sources = {
            semantic: {
                "provider": item.get("provider"),
                "source_timestamp": item.get("source_timestamp"),
                "received_at": item.get("received_at"),
                "provisional": item.get("provisional", False),
                "quality_flags": item.get("quality_flags", []),
                "comparison": item.get("comparison"),
            }
            for semantic, item in results.items()
        }
        provisional_reasons = []
        for semantic, item in results.items():
            if item.get("provisional") or item.get("quality_flags"):
                provisional_reasons.extend(
                    [f"{semantic}:{flag}" for flag in item.get("quality_flags", [])]
                )
        if not qvix:
            provisional_reasons.append("qvix_unavailable")
        if proxy_curve is None:
            provisional_reasons.append("proxy_curve_unavailable_bootstrap")
        sources["reference_curve"] = {
            "provider": proxy_curve["source"] if proxy_curve else "structural_bootstrap",
            "source_timestamp": proxy_curve["updated_at"] if proxy_curve else None,
            "received_at": proxy_curve["updated_at"] if proxy_curve else None,
            "provisional": proxy_curve is None,
            "quality_flags": [] if proxy_curve else ["proxy_curve_unavailable_bootstrap"],
            "comparison": None,
            "mode": proxy_mode,
            "blend_mode": curve_mode,
            "self_curve_weight": curve_weight,
            "sample_days": int(proxy_curve["sample_days"]) if proxy_curve else 0,
        }
        return AggregateSnapshot(
            trade_date=context.expected_trade_date,
            timestamp=context.now,
            phase=context.phase,
            session_minute=context.session_minute or 0,
            bucket_5m=context.bucket_5m or 0,
            index_symbol=str(index.get("symbol", "sh000300")),
            index_open=_required_number(index.get("open"), "指数开盘"),
            index_high=_required_number(index.get("high"), "指数最高"),
            index_low=_required_number(index.get("low"), "指数最低"),
            index_last=_required_number(index.get("last"), "指数现价"),
            index_previous_close=_required_number(index.get("previous_close"), "指数昨收"),
            index_volume=index.get("volume"),
            index_amount=index.get("amount"),
            up_count=int(breadth["up_count"]),
            down_count=int(breadth["down_count"]),
            flat_count=int(breadth["flat_count"]),
            valid_stock_count=int(breadth["valid_stock_count"]),
            decline_share=float(breadth["decline_share"]),
            decline_3_share=float(breadth["decline_3_share"]),
            decline_5_share=float(breadth["decline_5_share"]),
            decline_7_share=float(breadth["decline_7_share"]),
            median_return=float(breadth["median_return"]),
            limit_up=int(limits["limit_up"]),
            limit_down=int(limits["limit_down"]),
            market_amount=market_amount,
            incremental_amount_5m=incremental,
            projected_full_day_amount=projected,
            expected_cumulative_share=expected_share,
            front_contract=front["symbol"],
            front_price=float(front["price"]),
            front_bid=front.get("bid"),
            front_ask=front.get("ask"),
            front_expiry=front["expiry"],
            next_contract=next_contract["symbol"] if next_contract else None,
            next_price=float(next_contract["price"]) if next_contract else None,
            next_bid=next_contract.get("bid") if next_contract else None,
            next_ask=next_contract.get("ask") if next_contract else None,
            next_expiry=next_contract["expiry"] if next_contract else None,
            qvix_symbol=qvix_data.get("symbol"),
            qvix=qvix_data.get("value"),
            qvix_previous_close=qvix_data.get("previous_close"),
            qvix_previous_5m=qvix_data.get("previous_5m"),
            daily_sigma=_required_number(baseline.get("daily_sigma"), "前一日波动率"),
            median_daily_market_amount_20=baseline.get("median_daily_market_amount_20"),
            sources=sources,
            provisional_reasons=sorted(set(provisional_reasons)),
        )

    def _score(
        self,
        aggregate: AggregateSnapshot,
        current_day_history: list[dict[str, Any]],
        previous_bucket: dict[str, Any] | None,
        now: datetime,
    ) -> tuple[RealtimeResult, int, float]:
        values = build_realtime_feature_values(
            aggregate,
            current_day_history,
            previous_bucket,
            int(self.settings.get("futures.annualization_days")),
        )
        anchors = self.settings.section("fixed_anchors")
        all_scores: dict[str, float | None] = {}
        for name, value in values.items():
            if value is None or name not in anchors:
                all_scores[name] = None
            else:
                all_scores[name] = score_from_anchors(value, anchors[name])
        history = self.database.same_bucket_history(aggregate.bucket_5m, aggregate.trade_date)
        history_days = len(history)
        ref_mode, blend_weight = reference_state(history_days, self.settings.section("reference"))
        for name, structural in list(all_scores.items()):
            if structural is None:
                continue
            historical_scores = [
                float(row["feature_scores"].get(name))
                for row in history
                if row["feature_scores"].get(name) is not None
            ]
            if historical_scores:
                all_scores[name] = blend_with_history(
                    structural, structural, historical_scores, blend_weight
                )
        latest = self.database.latest_realtime()
        if (
            latest
            and latest["trade_date"] == aggregate.trade_date.isoformat()
            and int(latest["bucket_5m"]) == aggregate.bucket_5m
        ):
            fast_features = {"down_shock_5m_z"}
            for name in all_scores:
                if name not in fast_features:
                    all_scores[name] = latest["feature_scores"].get(name)
                    values[name] = latest["feature_values"].get(name)
        feature_groups = {
            "volatility": [
                "gap_down_z", "decline_from_previous_close_z", "realized_vol_so_far_z",
                "downside_vol_so_far_z", "range_so_far_z", "down_shock_5m_z",
            ],
            "breadth": [
                "decline_share", "severe_decline_share", "extreme_decline_share",
                "median_return_stress", "limit_down_intensity", "limit_imbalance",
            ],
            "derivatives": [
                "front_annualized_basis", "next_annualized_basis", "basis_curve_stress",
                "basis_widening_5m", "qvix_level", "qvix_change_from_previous_close",
                "qvix_change_5m",
            ],
            "liquidity": [
                "projected_amount_shortfall", "incremental_5m_illiquidity",
                "downside_turnover_shock", "amount_acceleration_stress",
            ],
        }
        components: dict[str, float | None] = {}
        feature_contributions: dict[str, float] = {}
        for group, names in feature_groups.items():
            weights = self._group_weights(
                group, names, aggregate.session_minute
            )
            group_scores = {name: all_scores.get(name) for name in names}
            components[group], _ = weighted_available_score(group_scores, weights)
            if components[group] is not None:
                for name in names:
                    if all_scores.get(name) is not None:
                        feature_contributions[name] = float(all_scores[name]) * float(weights[name])
        quality_reasons = list(aggregate.provisional_reasons)
        if aggregate.qvix is None:
            quality_reasons.append("qvix_unavailable")
        if ref_mode == "structural_bootstrap":
            quality_reasons.append("structural_bootstrap")
        base_daily = self.database.latest_daily(aggregate.trade_date)
        base_components = base_daily.get("components", {}) if base_daily else {}
        classification_quality = "complete" if base_daily else "cold_start"
        progress = max(aggregate.session_minute / 241.0, 1.0 / 241.0)
        beta = 0.65 + 0.35 * progress**0.5
        realtime_components = {}
        for group, value in components.items():
            if value is None:
                realtime_components[group] = None
                continue
            base = float(base_components.get(group, 50.0))
            realtime_components[group] = max(0.0, min(100.0, base + beta * (value - 50.0)))
        component_weights = self.settings.section("component_weights")
        raw, component_coverage = generalized_mean(
            realtime_components,
            component_weights,
            float(self.settings.get("model.generalized_mean_power")),
            float(self.settings.get("quality.provisional_min_coverage")),
        )
        coverage = self._feature_coverage(all_scores)
        minimum_coverage = float(
            self.settings.get("quality.provisional_min_coverage")
        )
        if raw is None or component_coverage < minimum_coverage or coverage < minimum_coverage:
            missing = [name for name, value in realtime_components.items() if value is None]
            raise IncompleteDataError(
                f"可用底层特征权重不足80%，coverage={coverage:.3f}: "
                + ", ".join(missing)
            )
        previous = self.database.latest_realtime(before=aggregate.timestamp.isoformat())
        previous_display = previous.get("realtime_panic_index") if previous else None
        display = smooth_display(
            raw,
            previous_display,
            float(self.settings.get("realtime.fast_rise_bypass_threshold")),
            float(self.settings.get("realtime.rising_alpha")),
            float(self.settings.get("realtime.falling_alpha")),
        )
        source_timestamps = {
            name: str(meta.get("source_timestamp"))
            for name, meta in aggregate.sources.items()
            if meta.get("source_timestamp")
        }
        confidence = min(100.0, coverage * 100.0)
        if aggregate.qvix is None:
            confidence = min(confidence, float(self.settings.get("quality.qvix_missing_confidence_cap")))
        if ref_mode == "structural_bootstrap":
            confidence = min(confidence, float(self.settings.get("quality.bootstrap_confidence_cap")))
        quality = "complete" if not quality_reasons and coverage >= 0.95 else "provisional"
        result = RealtimeResult(
            trade_date=aggregate.trade_date,
            timestamp=aggregate.timestamp,
            bucket_5m=aggregate.bucket_5m,
            realtime_panic_index_raw=raw,
            realtime_panic_index=display,
            level=classify_level(display),
            components={name: float(value) for name, value in realtime_components.items() if value is not None},
            feature_values=values,
            feature_scores=all_scores,
            feature_contributions=feature_contributions,
            confidence=confidence,
            coverage=coverage,
            reference_mode=ref_mode,
            classification_quality=classification_quality,
            quality_status=quality,
            missing_features=[name for name, value in all_scores.items() if value is None],
            stale_sources=[],
            provisional_reasons=sorted(set(quality_reasons)),
            source_timestamps=source_timestamps,
            source_skew_seconds=self._source_skew(
                {
                    name: self._effective_source_timestamp(
                        timestamp, self._aggregate_freshness_clock(aggregate)
                    )
                    for name, timestamp in source_timestamps.items()
                    if name in {"index", "breadth", "limits", "futures"}
                }
            ),
        )
        return result, history_days, blend_weight

    def _group_weights(
        self, group: str, names: list[str], session_minute: int
    ) -> dict[str, float]:
        configured = self.settings.section("feature_weights")
        if group in {"derivatives", "volatility", "breadth", "liquidity"}:
            values = configured.get(group, {})
        else:
            values = {}
        if group == "derivatives":
            values = {
                "front_annualized_basis": 0.33,
                "next_annualized_basis": 0.12,
                "basis_curve_stress": 0.09,
                "basis_widening_5m": 0.06,
                "qvix_level": 0.22,
                "qvix_change_from_previous_close": 0.12,
                "qvix_change_5m": 0.06,
            }
        weights = {name: float(values.get(name, 1.0 / len(names))) for name in names}
        if group == "breadth" and session_minute < 30:
            maturity = max(0.25, min(1.0, session_minute / 30.0))
            limit_names = {"limit_down_intensity", "limit_imbalance"}
            removed = 0.0
            for name in limit_names:
                original = weights[name]
                weights[name] = original * maturity
                removed += original - weights[name]
            other_names = [name for name in names if name not in limit_names]
            other_total = sum(weights[name] for name in other_names)
            for name in other_names:
                weights[name] += removed * weights[name] / other_total
        return weights

    def _same_bucket_days(self, bucket: int, before_date: date) -> int:
        return len(self.database.same_bucket_history(bucket, before_date))

    def _feature_coverage(self, scores: dict[str, float | None]) -> float:
        components = self.settings.section("component_weights")
        configured = self.settings.section("feature_weights")
        weights: dict[str, float] = {}
        for name, weight in configured["volatility"].items():
            weights[name] = components["volatility"] * float(weight)
        for name, weight in configured["breadth"].items():
            weights[name] = components["breadth"] * float(weight)
        for name, weight in configured["liquidity"].items():
            weights[name] = components["liquidity"] * float(weight)
        derivative_weight = components["derivatives"]
        for name, weight in configured["if_pressure"].items():
            weights[name] = (
                derivative_weight
                * float(configured["derivatives"]["if_pressure"])
                * float(weight)
            )
        for name, weight in configured["implied_volatility"].items():
            weights[name] = (
                derivative_weight
                * float(configured["derivatives"]["implied_volatility"])
                * float(weight)
            )
        total = sum(weights.values())
        available = sum(
            weight for name, weight in weights.items() if scores.get(name) is not None
        )
        return 0.0 if total <= 0 else available / total

    def _stale_sources(self, results: dict[str, dict[str, Any]], now: datetime) -> list[str]:
        limits = self.settings.section("freshness")
        mapping = {
            "index": "index_max_age_seconds",
            "breadth": "breadth_max_age_seconds",
            "limits": "limits_max_age_seconds",
            "futures": "futures_max_age_seconds",
        }
        stale = []
        for semantic, key in mapping.items():
            timestamp = results[semantic].get("source_timestamp")
            if not timestamp:
                stale.append(semantic)
                continue
            source = datetime.fromisoformat(timestamp)
            age = max(0.0, (now - source).total_seconds())
            if age > float(limits[key]):
                stale.append(f"{semantic}({age:.0f}s)")
        return stale

    def _drop_stale_optional_sources(
        self, results: dict[str, dict[str, Any]], now: datetime
    ) -> None:
        qvix = results.get("qvix")
        if not qvix or not qvix.get("source_timestamp"):
            results.pop("qvix", None)
            return
        source = datetime.fromisoformat(str(qvix["source_timestamp"]))
        age = max(0.0, (now - source).total_seconds())
        if age > float(self.settings.get("freshness.qvix_max_age_seconds")):
            results.pop("qvix", None)

    @staticmethod
    def _source_skew(timestamps: dict[str, str]) -> float:
        if len(timestamps) < 2:
            return 0.0
        values = [datetime.fromisoformat(item) for item in timestamps.values()]
        return (max(values) - min(values)).total_seconds()

    @staticmethod
    def _freshness_clock(context) -> datetime:
        if context.phase in {"finalizing", "closed_final"}:
            return datetime.combine(
                context.expected_trade_date,
                time(15, 0),
                tzinfo=context.now.tzinfo,
            )
        return context.now

    @staticmethod
    def _aggregate_freshness_clock(aggregate: AggregateSnapshot) -> datetime:
        if aggregate.phase in {"finalizing", "closed_final"}:
            return datetime.combine(
                aggregate.trade_date,
                time(15, 0),
                tzinfo=aggregate.timestamp.tzinfo,
            )
        return aggregate.timestamp

    @staticmethod
    def _effective_source_timestamp(value: str, ceiling: datetime) -> str:
        timestamp = datetime.fromisoformat(value)
        if timestamp.date() == ceiling.date() and timestamp > ceiling:
            timestamp = ceiling
        return timestamp.isoformat()


def _required_number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise IncompleteDataError(f"{name}缺失或无效") from error
    if not isfinite(result):
        raise IncompleteDataError(f"{name}缺失或无效")
    return result
