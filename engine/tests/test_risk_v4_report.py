"""交付报告只导出公开审计信息的形状测试。"""

from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.a_share_panic_index.risk_v4.providers import SYMBOLS
from scripts.a_share_panic_index.risk_v4.store import RiskStore
from scripts.risk_v4_report import EVENT_FIELDS, export_report


class ReportExportTests(unittest.TestCase):
    def test_all_registered_symbols_and_public_only_fund_information_are_exported(self):
        with tempfile.TemporaryDirectory(prefix="risk-v4-report-") as directory:
            root = Path(directory)
            database = root / "audit.db"
            output = root / "report"
            store = RiskStore(database)
            symbol = next(iter(SYMBOLS))
            store.ingest(symbol, {
                "source_id": "test_public_source", "fetched_at": "2026-09-12T08:00:00+00:00",
                "rows": [{"trade_date": "2026-09-11", "open": 1.0, "high": 1.1, "low": .9,
                          "close": 1.05, "amount": 100.0, "published_at": None, "pit_verified": False}],
            })
            store.put("state:" + symbol, {"state": "ready", "score": 12.3, "missing": []})
            store.put("model:" + symbol, {
                "artifact": {"trained_through": "2025-12-31", "calibrated_through": "2026-06-30",
                             "configuration_sha256": "test-config"},
                "validation": {"pit_verified": False, "oos_samples": 5, "reasons": ["发布时间未核实"],
                               "events": {"20d_5pct": {"positives": 1, "negatives": 4, "brier": .2,
                                                           "baseline_brier": .25, "simple_volatility_brier": .22,
                                                           "ece": .1, "passed": False}}},
                "strategy": {"selection_rule": "开发段固定公式", "baseline": {"development": {"net_return": .1,
                             "max_drawdown": -.05, "turnover": .2}, "holdout": {"turnover": .3}},
                             "grid": [{"development_selection_score": .4, "development_drawdown_improvement": .02}]},
            })
            store.put("watch_funds", ["013273", "004070"])
            store.put("fund:013273", {
                "history": {"available": True, "history": [{"trade_date": "2026-09-11", "unit_nav": 1.0}],
                            "total_return": {"available": False, "series": [], "reason": "未核实分红",
                                             "gaps": [{"effective_date": "2026-01-02", "raw_detail": "不应出现在Markdown" * 200},
                                                      {"effective_date": "2026-02-03", "raw_detail": "不应出现在Markdown" * 200}]},
                            "completeness": {"unit_nav_verified": True}, "source": {"provider": "akshare"}},
                "metadata": {"code": "013273", "name": "公开指数基金", "metadata_verified": True,
                             "benchmark_mapping": {"verified": True, "symbol": "cn399983"}},
            })
            store.put("portfolio", {"lots": [{"market_value": 987654321, "shares": 100}]})
            store.put("auxiliary", {"trade_date": "2026-09-11", "fetched_at": "2026-09-12T08:00:00+00:00",
                                    "limits": {"state": "ready", "source_id": "eastmoney", "upstream": "公开接口",
                                               "timestamp": None, "quality_flags": ["unverified_time"]}})

            result = export_report(database, output)

            self.assertEqual(result["symbols"], len(SYMBOLS))
            self.assertEqual(result["public_funds"], 2)
            self.assertNotIn(str(database), json.dumps(result, ensure_ascii=False))
            with (output / "风险模型事件验收.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["标的"], symbol)
            self.assertEqual(list(rows[0]), EVENT_FIELDS)
            self.assertEqual(rows[0]["简单波动基线Brier"], "0.22")
            manifest = json.loads((output / "模型参数与验证.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["注册标的日线审计"]), len(SYMBOLS))
            self.assertEqual(manifest["注册标的日线审计"][1]["记录数"], 0)
            self.assertEqual(manifest["公开基金"][1]["基金代码"], "004070")
            self.assertIn("尚无该公开基金的刷新记录", manifest["公开基金"][1]["缺项"])
            self.assertEqual(manifest["辅助观测"]["项目"]["limits"]["来源时间"], None)
            self.assertEqual(manifest["公开基金"][0]["复权校验"]["缺口"][0]["raw_detail"], "不应出现在Markdown" * 200)
            report_text = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
            self.assertNotIn("987654321", report_text)
            self.assertNotIn(str(database), report_text)
            self.assertIn("risk_v4_report.py", manifest["源码SHA256"])
            markdown = (output / "回测与实网验收.md").read_text(encoding="utf-8")
            self.assertNotIn("raw_detail", markdown)
            self.assertNotIn("不应出现在Markdown", markdown)
            self.assertIn("复权缺口：2026-01-02、2026-02-03（共2项）", markdown)
            strategy = manifest["模型参数与验证"][symbol]["策略"]
            self.assertEqual(strategy["baseline"]["development"]["turnover"], .2)
            self.assertEqual(strategy["grid"][0]["development_selection_score"], .4)


if __name__ == "__main__":
    unittest.main()
