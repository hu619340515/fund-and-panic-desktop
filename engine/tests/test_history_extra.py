from __future__ import annotations

try:
    import _bootstrap
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import io
import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.a_share_panic_index.providers import history_extra
from scripts.a_share_panic_index.providers.base import ProviderDataError


def _cffex_zip(day: str, rows: list[str] | None = None) -> bytes:
    csv_rows = rows or [
        "IO2609-C-4500,1,2,3,4,5,6,7,99,98,97,0,0,0.1",
        "IF2609,4535.6,4565.8,4517.6,55798,1,107146,2,4542.2,4539,4500,1,2,--",
        "IF2610,4510,4520,4490,1234,1,4567,2,4501.0,4500,4490,1,2,--",
        "IF2612,4510,4520,4490,1234,1,4567,2,null,4500,4490,1,2,--",
    ]
    header = (
        "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,"
        "今收盘,今结算,前结算,涨跌1,涨跌2,Delta"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{day.replace('-', '')}_1.csv",
            (header + "\n" + "\n".join(csv_rows)).encode("gb18030"),
        )
    return output.getvalue()


def _breadth_payload(day: str) -> dict:
    return {
        "errcode": "0",
        "date": day,
        "info": {
            "SZJS": "955",
            "XDJS": "4512",
            "0": "82",
            "ZT": "40",
            "DT": "13",
            "SJZT": "35",
            "STZT": "5",
        },
    }


class TestHistoryExtra(unittest.TestCase):
    def test_parses_real_cffex_chinese_header_and_only_uses_close(self):
        rows = history_extra._parse_cffex_zip(_cffex_zip("2026-09-10"), "202609")

        self.assertEqual(
            rows["2026-09-10"],
            [
                {"symbol": "IF2609", "last": 4542.2},
                {"symbol": "IF2610", "last": 4501.0},
            ],
        )
        self.assertNotEqual(rows["2026-09-10"][0]["last"], 4535.6)

    def test_breadth_rejects_wrong_date_empty_info_and_invalid_counts(self):
        wrong_day = _breadth_payload("2026-09-09")
        with self.assertRaisesRegex(ProviderDataError, "响应日期错位"):
            history_extra._parse_breadth_payload(wrong_day, "2026-09-10")
        with self.assertRaisesRegex(ProviderDataError, "info 为空"):
            history_extra._parse_breadth_payload(
                {"errcode": "0", "date": "2026-09-10", "info": {}},
                "2026-09-10",
            )
        invalid = _breadth_payload("2026-09-10")
        invalid["info"]["XDJS"] = "NaN"
        with self.assertRaisesRegex(ProviderDataError, "XDJS"):
            history_extra._parse_breadth_payload(invalid, "2026-09-10")

    def test_fetch_never_borrows_numbers_from_another_day(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(
            history_extra,
            "_download_cffex_month",
            return_value=_cffex_zip("2026-09-10"),
        ), patch.object(
            history_extra,
            "_download_breadth_payload",
            return_value=_breadth_payload("2026-09-09"),
        ):
            result = history_extra.fetch_extra_history(
                ["2026-09-10"], date(2026, 9, 11)
            )

        self.assertEqual(result["breadth"], {})
        self.assertIn("响应日期错位", ";".join(result["errors"]))

    def test_ended_month_and_daily_response_are_read_from_cache(self):
        cffex_calls: list[str] = []
        breadth_calls: list[str] = []

        def download_cffex(month: str, _timeout: float) -> bytes:
            cffex_calls.append(month)
            return _cffex_zip("2026-08-03")

        def download_breadth(day: str, _timeout: float) -> dict:
            breadth_calls.append(day)
            return _breadth_payload(day)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(
            history_extra, "_download_cffex_month", side_effect=download_cffex
        ), patch.object(
            history_extra, "_download_breadth_payload", side_effect=download_breadth
        ):
            first = history_extra.fetch_extra_history(
                ["2026-08-03"], date(2026, 9, 11)
            )
            second = history_extra.fetch_extra_history(
                ["2026-08-03"], date(2026, 9, 11)
            )
            cache_files = list((Path(directory) / "history-extra").glob("*.json"))
            temporary_files = list((Path(directory) / "history-extra").glob("*.tmp"))

        self.assertEqual(cffex_calls, ["202608"])
        self.assertEqual(breadth_calls, ["2026-08-03"])
        self.assertEqual(first["futures"], second["futures"])
        self.assertEqual(first["breadth"], second["breadth"])
        self.assertEqual(second["sources"]["futures"]["cached_months"], ["202608"])
        self.assertEqual(second["sources"]["breadth"]["cached_days"], 1)
        self.assertEqual(len(cache_files), 2)
        self.assertEqual(temporary_files, [])

    def test_current_month_is_refreshed_but_breadth_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(
            history_extra,
            "_download_cffex_month",
            return_value=_cffex_zip("2026-09-10"),
        ) as cffex, patch.object(
            history_extra,
            "_download_breadth_payload",
            return_value=_breadth_payload("2026-09-10"),
        ) as breadth:
            history_extra.fetch_extra_history(["2026-09-10"], date(2026, 9, 11))
            history_extra.fetch_extra_history(["2026-09-10"], date(2026, 9, 11))

        self.assertEqual(cffex.call_count, 2)
        self.assertEqual(breadth.call_count, 1)

    def test_partial_current_month_cache_is_completed_after_month_rolls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(
            history_extra,
            "_download_cffex_month",
            return_value=_cffex_zip("2026-09-10"),
        ) as cffex, patch.object(
            history_extra,
            "_download_breadth_payload",
            return_value=_breadth_payload("2026-09-10"),
        ):
            history_extra.fetch_extra_history(["2026-09-10"], date(2026, 9, 11))
            history_extra.fetch_extra_history(["2026-09-10"], date(2026, 10, 1))
            third = history_extra.fetch_extra_history(
                ["2026-09-10"], date(2026, 10, 2)
            )

        self.assertEqual(cffex.call_count, 2)
        self.assertEqual(third["sources"]["futures"]["cached_months"], ["202609"])

    def test_expiry_uses_last_official_appearance_in_completed_month(self):
        june = {
            "2026-06-18": [{"symbol": "IF2606", "last": 4000.0}],
            "2026-06-19": [{"symbol": "IF2607", "last": 4010.0}],
            "2026-06-22": [
                {"symbol": "IF2606", "last": 4020.0},
                {"symbol": "IF2607", "last": 4030.0},
            ],
        }
        futures = {
            "2026-05-29": [
                {"symbol": "IF2606", "last": 3990.0},
                {"symbol": "IF2609", "last": 3980.0},
            ]
        }

        expiries = history_extra._derive_official_expiries(
            {"202606": june}, {"202606"}
        )
        history_extra._annotate_official_expiries(futures, expiries)

        self.assertEqual(futures["2026-05-29"][0]["expiry"], "2026-06-22")
        self.assertNotIn("expiry", futures["2026-05-29"][1])

        damaged = {"2026-06-01": [{"symbol": "IF2606", "last": 3900.0}]}
        self.assertEqual(
            history_extra._derive_official_expiries(
                {"202606": damaged}, {"202606"}
            ),
            {},
        )

    def test_as_of_and_future_dates_are_not_requested(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"PANIC_INDEX_CACHE_DIRECTORY": directory},
        ), patch.object(history_extra, "_download_cffex_month") as cffex, patch.object(
            history_extra, "_download_breadth_payload"
        ) as breadth:
            result = history_extra.fetch_extra_history(
                ["2026-09-11", "2026-09-12", "bad"], date(2026, 9, 11)
            )

        cffex.assert_not_called()
        breadth.assert_not_called()
        self.assertEqual(result["futures"], {})
        self.assertEqual(result["breadth"], {})
        self.assertEqual(len(result["errors"]), 3)


if __name__ == "__main__":
    unittest.main()
