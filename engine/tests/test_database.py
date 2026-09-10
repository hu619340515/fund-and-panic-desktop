from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import sqlite3
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.a_share_panic_index.database import Database
from scripts.a_share_panic_index.pipeline.realtime import RealtimePipeline
from tests.helpers import REALTIME_FIXTURE, make_database, now, settings, test_logger


class TestDatabase(unittest.TestCase):
    def test_legacy_database_is_backed_up_and_replaced_by_v5(self):
        with tempfile.TemporaryDirectory(prefix="恐慌指数数据库-") as directory:
            root = Path(directory)
            path = root / "旧数据库.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE panic_index(date TEXT PRIMARY KEY, legacy_flow REAL)")
                connection.execute("INSERT INTO panic_index VALUES ('2026-07-23', 1.0)")
                connection.commit()
            database = Database(path, root / "备份")
            self.assertIsNotNone(database.last_backup)
            self.assertTrue(database.last_backup.exists())
            with closing(sqlite3.connect(path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                schema = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                all_columns = {
                    row[1]
                    for table in tables
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
            self.assertEqual(schema, "5")
            self.assertNotIn("panic_index", tables)
            self.assertNotIn("legacy_flow", all_columns)

    def test_wal_and_atomic_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_database(Path(directory))
            pipeline = RealtimePipeline(settings(), database, test_logger())
            context = pipeline.calendar.context(now(10, 0))
            results, events = pipeline.providers.collect(
                context.now, context.expected_trade_date, str(REALTIME_FIXTURE)
            )
            aggregate = pipeline._build_aggregate(context, results)
            result, history_days, blend = pipeline._score(aggregate, [], None, context.now)
            with self.assertRaisesRegex(RuntimeError, "事务回滚"):
                database.write_realtime(
                    aggregate,
                    result,
                    history_days,
                    blend,
                    events,
                    fail_after_raw=True,
                )
            with closing(database.connect()) as connection:
                raw_count = connection.execute(
                    "SELECT COUNT(*) FROM realtime_raw_metrics"
                ).fetchone()[0]
                score_count = connection.execute(
                    "SELECT COUNT(*) FROM realtime_panic_index"
                ).fetchone()[0]
            self.assertEqual(database.journal_mode(), "wal")
            self.assertEqual(raw_count, 0)
            self.assertEqual(score_count, 0)

    def test_legacy_wal_commits_are_kept_in_migration_backup(self):
        with tempfile.TemporaryDirectory(prefix="WAL迁移备份-") as directory:
            root = Path(directory)
            path = root / "旧版.db"
            source = root / "运行中旧版.db"
            writer = sqlite3.connect(source)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE legacy(value TEXT)")
            writer.execute("INSERT INTO legacy VALUES ('WAL内已提交记录')")
            writer.commit()
            source_wal = Path(str(source) + "-wal")
            self.assertTrue(source_wal.exists())
            shutil.copy2(source, path)
            shutil.copy2(source_wal, Path(str(path) + "-wal"))
            writer.close()

            database = Database(path, root / "备份")

            self.assertIsNotNone(database.last_backup)
            with closing(sqlite3.connect(database.last_backup)) as connection:
                value = connection.execute("SELECT value FROM legacy").fetchone()[0]
            self.assertEqual(value, "WAL内已提交记录")

    def test_same_minute_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_database(Path(directory))
            pipeline = RealtimePipeline(settings(), database, test_logger())
            pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
            pipeline.run(now(10, 0), fixture=str(REALTIME_FIXTURE))
            with closing(database.connect()) as connection:
                counts = [
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "realtime_raw_metrics",
                        "realtime_features",
                        "realtime_panic_index",
                        "intraday_aggregate_snapshots",
                    )
                ]
            self.assertEqual(counts, [1, 1, 1, 1])

    def test_chinese_path_can_be_created_and_read(self):
        with tempfile.TemporaryDirectory(prefix="中文路径-") as directory:
            database = make_database(Path(directory), "市场压力.db")
            RealtimePipeline(settings(), database, test_logger()).run(
                now(10, 0), fixture=str(REALTIME_FIXTURE)
            )
            self.assertIsNotNone(database.latest_realtime())


if __name__ == "__main__":
    unittest.main()
