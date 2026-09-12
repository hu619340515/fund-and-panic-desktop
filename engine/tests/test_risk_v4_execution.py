"""公开申赎状态与交易日历不应推出未知的成交/确认条件。"""

from __future__ import annotations

try:
    import _bootstrap
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import hashlib
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from scripts.a_share_panic_index.risk_v4 import execution


def _source_row(code="000001", kind="债券型", buy="开放申购", sell="开放赎回", next_day="", limit=""):
    # 对应 AKShare fund_purchase_em 原始 datas 的13列，索引尚未插入。
    return [code, "测试标准基金", kind, "1.0", "2026-09-11", buy, sell, next_day,
            "100", limit, "-", "-", "0.5"]


def _terms(row, when="2026-09-14T14:59:00", metadata=None):
    with patch.object(execution, "_request_table", return_value=([row], "example-hash")):
        return execution.fetch_execution_terms("000001", metadata or {}, datetime.fromisoformat(when))


class ExecutionTests(unittest.TestCase):
    def test_open_fund_before_and_at_cutoff_has_distinct_submission_day(self):
        before=_terms(_source_row())
        self.assertTrue(before["verified"])
        self.assertEqual(before["state"],"open")
        self.assertEqual(before["next_order_date"],"2026-09-14")
        self.assertEqual(before["next_subscription_date"],"2026-09-14")
        self.assertIsNone(before["confirmation_days"])
        self.assertIn("未来成交净值",before["missing"])
        after=_terms(_source_row(), "2026-09-14T15:00:00")
        self.assertEqual(after["next_order_date"],"2026-09-15")
        weekend=_terms(_source_row(), "2026-09-12T11:00:00")
        self.assertEqual(weekend["next_order_date"],"2026-09-14")

    def test_one_side_closed_never_creates_buy_date(self):
        result=_terms(_source_row(buy="暂停申购"))
        self.assertTrue(result["verified"])
        self.assertEqual(result["state"],"partially_open")
        self.assertFalse(result["subscription_open"])
        self.assertIsNone(result["next_subscription_date"])
        self.assertEqual(result["next_redemption_date"],"2026-09-14")

    def test_unknown_limit_and_qdii_cannot_use_domestic_submission_rule(self):
        limited=_terms(_source_row(limit="10000"))
        self.assertTrue(limited["verified"])
        self.assertEqual(limited["state"],"open_with_limit")
        self.assertEqual(limited["next_order_date"],"2026-09-14")
        self.assertIn("日累计限额剩余及订单金额匹配",limited["missing"])
        foreign=_terms(_source_row(kind="QDII-股票型"))
        self.assertFalse(foreign["verified"])
        self.assertIsNone(foreign["subscription_open"])
        self.assertIn("日历",foreign["reason"])
        self.assertFalse(_terms(_source_row(kind="指数型"),metadata={"name":"跨境ETF"})["verified"])

    def test_source_future_open_day_must_be_verified_session(self):
        result=_terms(_source_row(next_day="2026-09-16"))
        self.assertTrue(result["verified"])
        self.assertEqual(result["next_order_date"],"2026-09-16")
        mismatch=_terms(_source_row(next_day="2026-09-19"))
        self.assertFalse(mismatch["verified"])
        self.assertIsNone(mismatch["next_order_date"])

    def test_unknown_or_duplicate_source_status_stays_unverified(self):
        partial=_terms(_source_row(buy="限大额申购"))
        self.assertTrue(partial["verified"])
        self.assertFalse(partial["subscription_verified"])
        self.assertTrue(partial["redemption_verified"])
        self.assertIsNone(partial["next_subscription_date"])
        self.assertEqual(partial["next_redemption_date"],"2026-09-14")
        self.assertFalse(_terms(_source_row(buy="限大额申购",sell="未知赎回"))["verified"])
        with patch.object(execution,"_request_table",return_value=([_source_row(),_source_row()],"sha")):
            duplicate=execution.fetch_execution_terms("000001",{},datetime.fromisoformat("2026-09-14T10:00"))
        self.assertFalse(duplicate["verified"])
        self.assertIn("唯一",duplicate["reason"])
        with patch.object(execution,"_request_table",side_effect=TimeoutError("timeout")):
            failed=execution.fetch_execution_terms("000001",{},datetime.fromisoformat("2026-09-14T10:00"))
        self.assertFalse(failed["verified"])
        self.assertIsNone(failed["next_order_date"])

    @patch("requests.get")
    def test_source_response_uses_akshare_schema_with_explicit_timeout(self, get):
        raw=("var reData="+json.dumps({"datas":[_source_row()]},ensure_ascii=False)+";").encode("utf-8")
        response=get.return_value
        response.iter_content.return_value=[raw]
        rows,digest=execution._request_table()
        self.assertEqual(rows[0][0],"000001")
        self.assertEqual(digest,hashlib.sha256(raw).hexdigest())
        self.assertEqual(get.call_args.kwargs["timeout"],(4,12))
        self.assertTrue(get.call_args.kwargs["stream"])
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
