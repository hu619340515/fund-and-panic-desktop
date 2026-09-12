"""第四版真实行情适配器的日期、精度和缺失语义测试。"""

from __future__ import annotations

try:
    import _bootstrap as _test_bootstrap
except ModuleNotFoundError:
    from . import _bootstrap as _test_bootstrap

_ = _test_bootstrap

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from scripts.a_share_panic_index.risk_v4 import nav, providers


def _row(day, *, close="100.00", amount=""):
    return {
        "trade_date": day,
        "open": "100.00",
        "high": "101.00",
        "low": "99.00",
        "close": close,
        "amount": amount,
    }


class TestProviders(unittest.TestCase):
    def test_hk_rounds_decoder_tail_without_inventing_amount(self):
        records, quality = providers.normalize_rows(
            [_row("2026-09-01", close="100.00001")],
            "hkHSTECH",
            "2026-09-01",
            "2026-09-01",
        )
        self.assertEqual(records[0]["close"], 100.0)
        self.assertIsNone(records[0]["amount"])
        self.assertEqual(quality["rejected"], [])
        self.assertEqual(quality["ohlc_rounding_repaired_dates"], ["2026-09-01"])

    def test_hk_official_weather_closures_are_not_data_gaps(self):
        rows = [
            _row("2023-08-31"), _row("2023-09-04"), _row("2023-09-05"),
            _row("2023-09-06"), _row("2023-09-07"),
        ]
        _, quality = providers.normalize_rows(rows, "hkHSI", "2023-08-31", "2023-09-08")
        self.assertNotIn("2023-09-01", quality["missing_dates"])
        self.assertNotIn("2023-09-08", quality["missing_dates"])
        self.assertEqual(set(quality["exceptional_closures"]), {"2023-09-01", "2023-09-08"})

    def test_expected_close_day_ignores_short_cached_calendar_range(self):
        # 先创建一个很短的日历，复现旧版 get_calendar(name) 的缓存触发条件。
        providers.normalize_rows([_row("2026-09-11")], "sh000300", "2026-09-11", "2026-09-11")
        now = datetime(2026, 9, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(providers.expected_close_day("sh000300", now), "2026-09-11")

    def test_non_session_source_row_is_rejected_without_calendar_bound_error(self):
        records, quality = providers.normalize_rows(
            [_row("2010-01-01"), _row("2010-01-04")],
            "sh000300",
            "2010-01-01",
            "2010-01-04",
        )
        self.assertEqual([record["trade_date"] for record in records], ["2010-01-04"])
        self.assertEqual(quality["non_session_rows"], ["2010-01-01"])

    def test_close_only_official_row_is_preserved_without_fabricating_ohlc(self):
        records, quality = providers.normalize_rows(
            [{"trade_date": "2019-03-20", "close": "1156.41", "amount": "10704000000"}],
            "cn931865",
            "2019-03-20",
            "2019-03-20",
        )
        self.assertEqual(records[0]["close"], 1156.41)
        self.assertIsNone(records[0]["open"])
        self.assertIsNone(records[0]["high"])
        self.assertIsNone(records[0]["low"])
        self.assertEqual(quality["close_only_dates"], ["2019-03-20"])

    def test_invalid_provided_ohlc_is_rejected_instead_of_repaired(self):
        bad = _row("2019-03-20")
        bad["low"] = "100.50"
        records, quality = providers.normalize_rows(
            [bad, _row("2019-03-21")], "cn931865", "2019-03-20", "2019-03-21",
        )
        self.assertEqual([item["trade_date"] for item in records], ["2019-03-21"])
        self.assertIn("OHLC关系非法", quality["rejected"][0]["error"])

    def test_industry_benchmark_requires_exact_verified_name(self):
        matched = providers.resolve_exact_benchmark("中证新能源指数")
        self.assertTrue(matched["verified"])
        self.assertEqual(matched["symbol"], "cn399808")
        self.assertIn("399808factsheet.pdf", matched["source_url"])
        self.assertFalse(providers.resolve_exact_benchmark("中证新能源指数收益率*95%")["verified"])

    def test_standard_index_fund_accepts_only_unique_explicit_target(self):
        mapped = providers.resolve_unique_fund_benchmark(
            "股票型-标准指数",
            "沪深300地产等权重指数收益率×95%+活期存款利率×5%",
            "采用完全复制法跟踪标的指数",
        )
        self.assertTrue(mapped["verified"])
        self.assertEqual(mapped["symbol"], "cn399983")
        self.assertFalse(mapped["pit_verified"])
        active = providers.resolve_unique_fund_benchmark(
            "股票型-普通", "中证500指数收益率*90%+活期存款利率*10%", "多因子选股",
        )
        self.assertFalse(active["verified"])

    def test_nav_metadata_maps_non_etf_standard_index_with_unique_target(self):
        class Akshare:
            @staticmethod
            def fund_individual_basic_info_xq(**_kwargs):
                return pd.DataFrame([
                    {"item": "基金名称", "value": "招商沪深300地产指数C"},
                    {"item": "基金全称", "value": "招商沪深300地产等权重指数证券投资基金"},
                    {"item": "基金类型", "value": "股票型-标准指数"},
                    {"item": "业绩比较基准", "value": "沪深300地产等权重指数收益率×95%+活期存款利率×5%"},
                    {"item": "投资策略", "value": "采用完全复制法跟踪标的指数"},
                ])

        with patch.object(nav, "_load_akshare", return_value=Akshare()):
            result = nav.fetch_fund_metadata("013273")
        self.assertTrue(result["verified"])
        self.assertEqual(result["symbol"], "cn399983")
        self.assertFalse(result["benchmark_mapping"]["pit_verified"])

    def test_industry_start_dates_are_publication_dates_not_base_dates(self):
        self.assertEqual(providers.SYMBOLS["cn399808"]["start"], "2015-02-10")
        self.assertEqual(providers.SYMBOLS["cn399989"]["start"], "2014-10-31")
        self.assertEqual(providers.SYMBOLS["cn931865"]["start"], "2019-03-20")

    def test_csindex_official_fallback_converts_yi_to_cny(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": "200",
                    "data": [{
                        "tradeDate": "20260911", "indexCode": "000300",
                        "indexNameEn": "CSI 300", "open": 4600.0,
                        "high": 4650.0, "low": 4590.0, "close": 4620.0,
                        "tradingValue": 5357.08,
                    }],
                }

        def fake_get(url, **_kwargs):
            if "eastmoney" in url:
                raise RuntimeError("东财断连")
            self.assertEqual(url, providers.CSI_PERFORMANCE_URL)
            return Response()

        with patch.object(providers.requests, "get", side_effect=fake_get):
            result = providers.fetch_history("sh000300", "2026-09-11", "2026-09-11")
        self.assertEqual(result["source_id"], "csindex")
        self.assertEqual(result["amount_unit"], "CNY")
        self.assertEqual(result["rows"][0]["amount"], 535_708_000_000.0)
        self.assertTrue(result["quality"]["amount_verified"])

    def test_csindex_keeps_ohlc_when_one_day_has_no_amount(self):
        meta = providers.SYMBOLS["sh000300"]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": "200",
                    "data": [{
                        "tradeDate": "20260911", "indexCode": "000300",
                        "indexNameEn": "CSI 300", "open": 4600.0,
                        "high": 4650.0, "low": 4590.0, "close": 4620.0,
                        "tradingValue": 0,
                    }],
                }

        with patch.object(providers.requests, "get", return_value=Response()):
            result = providers._fetch_csindex_history("sh000300", meta, "2026-09-11", "2026-09-11")
        self.assertIsNone(result["rows"][0]["amount"])
        self.assertFalse(result["quality"]["amount_verified"])
        self.assertEqual(result["quality"]["amount_missing_dates"], ["20260911"])

    def test_cnindex_official_history_keeps_unverified_amount_empty_and_uses_shanghai_date(self):
        stamp = int(datetime(2012, 12, 28, tzinfo=timezone.utc).timestamp() * 1000)

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "indexCode": "980092", "indexName": "自由现金流", "indexEName": "CNIFCF",
                        "item": ["timestamp", "current", "high", "open", "low", "close", "chg", "percent", "amount", "volume", "avg"],
                        "data": [[stamp, 100.0, 101.0, 99.0, 98.0, 100.0, 1.0, 1.0, 123456.0, 1000.0, 99.5]],
                    },
                }

        def fake_get(url, **kwargs):
            self.assertEqual(url, providers.CNI_DAILY_URL)
            self.assertEqual(kwargs["params"], {"indexCode": "980092", "startDate": "2012-12-28", "endDate": "2012-12-28"})
            self.assertEqual(kwargs["headers"]["Origin"], "https://www.cnindex.com.cn")
            self.assertEqual(kwargs["headers"]["Referer"], "https://www.cnindex.com.cn/")
            return Response()

        with patch.object(providers.requests, "get", side_effect=fake_get):
            result = providers.fetch_history("sz980092", "2012-12-28", "2012-12-28")
        self.assertEqual(result["source_id"], "cnindex")
        self.assertEqual(result["rows"][0]["trade_date"], "2012-12-28")
        self.assertEqual(result["rows"][0]["close"], 100.0)
        self.assertIsNone(result["rows"][0]["amount"])
        self.assertFalse(result["quality"]["amount_verified"])
        self.assertEqual(result["quality"]["amount_unverified_dates"], ["2012-12-28"])

    def test_auxiliary_sources_fail_independently(self):
        def fake_fetch(provider, semantic_type, context):
            if provider in {"qvix_300_index", "eastmoney"}:
                raise RuntimeError(f"{provider}暂不可用")
            flags = ["provider_timestamp_unavailable"] if semantic_type in {"breadth", "limits"} else []
            return {
                "provider": provider,
                "data": {"semantic_type": semantic_type, "trade_date": context["trade_date"]},
                "source_timestamp": "2026-09-11T15:10:00+08:00",
                "quality_flags": flags,
            }

        with patch("scripts.a_share_panic_index.providers.live.fetch_live", side_effect=fake_fetch):
            result = providers.fetch_auxiliary(trade_date="2026-09-11")
        self.assertEqual(result["qvix"]["state"], "ready")
        self.assertEqual(result["qvix"]["source_id"], "qvix_300_etf")
        self.assertEqual(result["futures"]["state"], "ready")
        self.assertEqual(result["breadth"]["source_id"], "tencent")
        self.assertIsNone(result["breadth"]["timestamp"])
        self.assertIn("unverified_time", result["breadth"]["quality_flags"])
        self.assertEqual(result["limits"]["state"], "ready")
        self.assertIsNone(result["limits"]["timestamp"])
        self.assertIn("unverified_time", result["limits"]["quality_flags"])
        self.assertEqual(len(result["attempts"]), 7)

    def test_auxiliary_rejects_if_without_same_day_source_time(self):
        def fake_fetch(provider, semantic_type, _context):
            if semantic_type == "futures":
                return {
                    "provider": provider, "data": {},
                    "source_timestamp": "2026-09-10T15:10:00+08:00",
                    "quality_flags": [],
                }
            return {
                "provider": provider, "data": {},
                "source_timestamp": "2026-09-11T15:10:00+08:00",
                "quality_flags": [],
            }

        with patch("scripts.a_share_panic_index.providers.live.fetch_live", side_effect=fake_fetch):
            result = providers.fetch_auxiliary(trade_date="2026-09-11")
        self.assertEqual(result["futures"]["state"], "unavailable")
        self.assertIn("同交易日", result["futures"]["error"])
