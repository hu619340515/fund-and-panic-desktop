"""收盘正式指数与自采参考曲线。"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from math import sqrt
from statistics import median
from typing import Any

from ..calendar import TradingCalendar
from ..features.daily import build_daily_feature_values
from ..models import DailyResult
from ..scoring import (
    classify_level,
    generalized_mean,
    historical_percentile,
    score_from_anchors,
    weighted_available_score,
)
from .realtime import IncompleteDataError, RealtimePipeline


class DailyPipeline:
    def __init__(self, settings, database, logger):
        self.settings = settings
        self.database = database
        self.logger = logger
        market = settings.section("market")
        self.calendar = TradingCalendar(
            market.get("calendar", "XSHG"), market.get("timezone", "Asia/Shanghai")
        )
        hour, minute = [int(part) for part in market.get("finalization_time", "15:10").split(":")]
        self.finalization_time = time(hour, minute)

    def run(
        self,
        now: datetime,
        requested_date: date | None = None,
        fixture: str | None = None,
    ) -> tuple[DailyResult | None, dict[str, Any]]:
        context = self.calendar.context(now, requested_date)
        self.logger.info(
            "daily开始 requested=%s expected=%s phase=%s fixture=%s",
            context.requested_date,
            context.expected_trade_date,
            context.phase,
            bool(fixture),
        )
        if not context.is_trading_day:
            return None, {
                "context": context.to_dict(),
                "status": "skipped_non_trading_day",
                "latest_daily": self.database.latest_daily(context.expected_trade_date),
            }
        if (
            context.requested_date == context.now.date()
            and context.now.time().replace(tzinfo=None) < self.finalization_time
        ):
            return None, {
                "context": context.to_dict(),
                "status": "market_not_ready",
                "latest_daily": self.database.latest_daily(
                    self.calendar.previous_session(context.expected_trade_date)
                ),
            }
        latest_aggregate = self.database.latest_closing_aggregate(
            context.expected_trade_date
        )
        latest_realtime = (
            self.database.realtime_at(
                context.expected_trade_date, latest_aggregate["timestamp"]
            )
            if latest_aggregate
            else None
        )
        if fixture or latest_aggregate is None or latest_realtime is None:
            realtime = RealtimePipeline(self.settings, self.database, self.logger)
            collection_time = now
            if fixture:
                collection_time = datetime.combine(
                    context.expected_trade_date,
                    self.finalization_time,
                    tzinfo=context.now.tzinfo,
                )
            realtime_result, _ = realtime.run(
                collection_time,
                context.expected_trade_date,
                fixture=fixture,
                persist=True,
            )
            latest_aggregate = self.database.latest_closing_aggregate(
                context.expected_trade_date
            )
            latest_realtime = (
                self.database.realtime_at(
                    context.expected_trade_date, latest_aggregate["timestamp"]
                )
                if latest_aggregate
                else None
            )
        if latest_aggregate is None or latest_realtime is None:
            raise IncompleteDataError(
                "缺少目标交易日15:00收盘聚合快照（session_minute=241, bucket_5m=48）"
            )
        if latest_aggregate["trade_date"] != context.expected_trade_date.isoformat():
            raise IncompleteDataError("最新聚合快照不是目标交易日")
        if (
            int(latest_aggregate["session_minute"]) != 241
            or int(latest_aggregate["bucket_5m"]) != 48
            or int(latest_realtime["bucket_5m"]) != 48
        ):
            raise IncompleteDataError("目标交易日收盘快照不完整，拒绝写入final")
        raw = self._build_raw(latest_aggregate, latest_realtime)
        raw_history = self.database.daily_raw_history(context.expected_trade_date)
        values = build_daily_feature_values(raw, raw_history)
        result = self._score(context.expected_trade_date, raw, values)
        self.database.write_daily(raw, result)
        self._refresh_self_curve()
        self.logger.info(
            "daily完成 trade_date=%s final=%.4f quality=%s",
            result.trade_date,
            result.final_panic_index,
            result.quality_status,
        )
        return result, {
            "context": context.to_dict(),
            "status": "success",
            "raw": raw,
            "backup": str(self.database.last_backup) if self.database.last_backup else None,
        }

    def _build_raw(
        self, aggregate: dict[str, Any], realtime: dict[str, Any]
    ) -> dict[str, Any]:
        features = realtime.get("feature_values", {})
        if not features and realtime.get("feature_values_json"):
            features = json.loads(realtime["feature_values_json"])
        sources = json.loads(aggregate["sources_json"])
        return {
            "trade_date": aggregate["trade_date"],
            "open": aggregate["index_open"],
            "high": aggregate["index_high"],
            "low": aggregate["index_low"],
            "close": aggregate["index_last"],
            "previous_close": aggregate["index_previous_close"],
            "up_count": aggregate["up_count"],
            "down_count": aggregate["down_count"],
            "flat_count": aggregate["flat_count"],
            "valid_stock_count": aggregate["valid_stock_count"],
            "decline_share": aggregate["decline_share"],
            "decline_5_share": aggregate["decline_5_share"],
            "decline_7_share": aggregate["decline_7_share"],
            "median_return": aggregate["median_return"],
            "limit_up": aggregate["limit_up"],
            "limit_down": aggregate["limit_down"],
            "market_amount": aggregate["market_amount"],
            "daily_sigma": aggregate["daily_sigma"],
            "front_contract": aggregate["front_contract"],
            "front_annualized_basis": features.get("front_annualized_basis"),
            "next_annualized_basis": features.get("next_annualized_basis"),
            "basis_curve_stress": features.get("basis_curve_stress"),
            "basis_expansion_3d": self._basis_expansion(features.get("front_annualized_basis")),
            "qvix": aggregate["qvix"],
            "qvix_daily_change": features.get("qvix_change_from_previous_close"),
            "sources": sources,
        }

    def _basis_expansion(self, current: float | None) -> float | None:
        if current is None:
            return None
        history = self.database.daily_raw_history(date.max, limit=3)
        old = [item.get("front_annualized_basis") for item in history]
        old = [float(item) for item in old if item is not None]
        return float(current) - old[0] if len(old) >= 3 else None

    def _score(
        self,
        trade_date: date,
        raw: dict[str, Any],
        values: dict[str, float | None],
    ) -> DailyResult:
        anchors = self.settings.section("fixed_anchors")
        annualized_baseline = max(float(raw["daily_sigma"]) * sqrt(252), 1e-9)
        transformations = {
            "ewma_volatility_5": ("realized_vol_so_far_z", _ratio(values.get("ewma_volatility_5"), annualized_baseline)),
            "realized_volatility_20": ("realized_vol_so_far_z", _ratio(values.get("realized_volatility_20"), annualized_baseline)),
            "downside_volatility_20": ("downside_vol_so_far_z", _ratio(values.get("downside_volatility_20"), annualized_baseline)),
            "parkinson_volatility_10": ("range_so_far_z", _ratio(values.get("parkinson_volatility_10"), annualized_baseline)),
            "daily_down_jump": ("gap_down_z", _ratio(values.get("daily_down_jump"), float(raw["daily_sigma"]))),
            "decline_share": ("decline_share", values.get("decline_share")),
            "severe_decline_share": ("severe_decline_share", values.get("severe_decline_share")),
            "extreme_decline_share": ("extreme_decline_share", values.get("extreme_decline_share")),
            "median_return_stress": ("median_return_stress", values.get("median_return_stress")),
            "limit_down_intensity": ("limit_down_intensity", values.get("limit_down_intensity")),
            "limit_imbalance": ("limit_imbalance", values.get("limit_imbalance")),
            "front_annualized_basis": ("front_annualized_basis", values.get("front_annualized_basis")),
            "next_annualized_basis": ("next_annualized_basis", values.get("next_annualized_basis")),
            "basis_curve_stress": ("basis_curve_stress", values.get("basis_curve_stress")),
            "basis_expansion_3d": ("basis_widening_5m", values.get("basis_expansion_3d")),
            "qvix_level": ("qvix_level", values.get("qvix_level")),
            "qvix_daily_change": ("qvix_change_from_previous_close", values.get("qvix_daily_change")),
            "daily_amount_shortfall": ("projected_amount_shortfall", values.get("daily_amount_shortfall")),
            "daily_amihud": ("incremental_5m_illiquidity", values.get("daily_amihud")),
            "daily_downside_turnover": ("downside_turnover_shock", values.get("daily_downside_turnover")),
        }
        scores: dict[str, float | None] = {}
        history = self.database.daily_feature_history(trade_date)
        for name, (anchor_name, transformed) in transformations.items():
            if transformed is None:
                scores[name] = None
                continue
            structural = score_from_anchors(float(transformed), anchors[anchor_name])
            past_scores = [
                item["feature_scores"].get(name)
                for item in history
                if item["feature_scores"].get(name) is not None
            ]
            percentile = historical_percentile(structural, past_scores)
            if percentile is not None and len(past_scores) >= 20:
                weight = min(0.5, 0.5 * (len(past_scores) - 20) / 40.0)
                structural = (1.0 - weight) * structural + weight * percentile
            scores[name] = structural
        groups = {
            "volatility": {
                "ewma_volatility_5": 0.25,
                "realized_volatility_20": 0.25,
                "downside_volatility_20": 0.20,
                "parkinson_volatility_10": 0.15,
                "daily_down_jump": 0.15,
            },
            "breadth": self.settings.section("feature_weights")["breadth"],
            "derivatives": {
                "front_annualized_basis": 0.30,
                "next_annualized_basis": 0.12,
                "basis_curve_stress": 0.10,
                "basis_expansion_3d": 0.08,
                "qvix_level": 0.25,
                "qvix_daily_change": 0.15,
            },
            "liquidity": {
                "daily_amount_shortfall": 0.40,
                "daily_amihud": 0.30,
                "daily_downside_turnover": 0.30,
            },
        }
        components: dict[str, float | None] = {}
        for group, weights in groups.items():
            components[group], _ = weighted_available_score(scores, weights)
        final_score, component_coverage = generalized_mean(
            components,
            self.settings.section("component_weights"),
            float(self.settings.get("model.generalized_mean_power")),
            float(self.settings.get("quality.provisional_min_coverage")),
        )
        component_weights = self.settings.section("component_weights")
        total_weight = 0.0
        available_weight = 0.0
        for group, weights in groups.items():
            for name, weight in weights.items():
                absolute = float(component_weights[group]) * float(weight)
                total_weight += absolute
                if scores.get(name) is not None:
                    available_weight += absolute
        coverage = available_weight / total_weight if total_weight else 0.0
        missing_components = [
            name for name, value in components.items() if value is None
        ]
        if final_score is None or component_coverage < 1.0 or missing_components:
            raise IncompleteDataError(
                "收盘四个一级组件不完整: " + ", ".join(missing_components)
            )
        source_timestamps = {
            name: str(meta.get("source_timestamp"))
            for name, meta in raw["sources"].items()
            if meta.get("source_timestamp")
        }
        qvix_missing = values.get("qvix_level") is None
        quality = "provisional" if qvix_missing or coverage < 0.95 else "complete"
        confidence = coverage * 100.0
        if qvix_missing:
            confidence = min(
                confidence,
                float(self.settings.get("quality.qvix_missing_confidence_cap")),
            )
        return DailyResult(
            trade_date=trade_date,
            final_panic_index=final_score,
            level=classify_level(final_score),
            components={name: float(value) for name, value in components.items() if value is not None},
            feature_values=values,
            feature_scores=scores,
            confidence=confidence,
            coverage=coverage,
            quality_status=quality,
            source_timestamps=source_timestamps,
        )

    def _refresh_self_curve(self) -> None:
        rows = self.database.aggregate_curve_rows(250)
        if not rows:
            return
        finals: dict[str, float] = {}
        for row in rows:
            finals[row["trade_date"]] = max(
                finals.get(row["trade_date"], 0.0), float(row["market_amount"])
            )
        by_bucket: dict[int, list[float]] = {}
        for row in rows:
            total = finals.get(row["trade_date"], 0.0)
            if total > 0:
                by_bucket.setdefault(int(row["bucket_5m"]), []).append(
                    float(row["market_amount"]) / total
                )
        curve = {bucket: median(values) for bucket, values in by_bucket.items() if values}
        if curve:
            self.database.upsert_reference_curve(
                "self_market", curve, len(finals), "self_collected_market"
            )


def _ratio(value: float | None, denominator: float) -> float | None:
    return None if value is None else float(value) / max(float(denominator), 1e-9)
