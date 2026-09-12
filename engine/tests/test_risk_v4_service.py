from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.a_share_panic_index.database import Database
from scripts.a_share_panic_index.risk_v4.api import router
from scripts.a_share_panic_index.risk_v4.service import RiskService, public_forecast, daily_return_filter, data_fingerprint
from scripts.a_share_panic_index.risk_v4.store import RiskStore


class TestMigrationAndAudit(unittest.TestCase):
    def test_v5_upgrade_backs_up_wal_and_keeps_legacy_and_portfolio(self):
        with tempfile.TemporaryDirectory(prefix="迁移中文-") as directory:
            path = Path(directory)/"data.db"
            Database(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
                connection.execute("PRAGMA user_version=5")
                connection.execute("CREATE TABLE user_marker(value TEXT)")
                connection.execute("INSERT INTO user_marker VALUES ('保留基金配置')")
                connection.commit()
                database = Database(path)
                self.assertTrue(database.last_backup.exists())
                for target in (path, database.last_backup):
                    with closing(sqlite3.connect(target)) as verification:
                        self.assertEqual(verification.execute("SELECT value FROM user_marker").fetchone()[0],"保留基金配置")
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0],6)
                self.assertIn("risk_bars",{row[0] for row in connection.execute("SELECT name FROM sqlite_master")})
                self.assertIsNone(Database(path).last_backup)

    def test_future_schema_is_read_only_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"future.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version=7")
                connection.execute("CREATE TABLE untouched(v)")
            with self.assertRaisesRegex(ValueError,"拒绝降级"):
                Database(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0],7)

    def test_raw_revisions_and_actual_publications_are_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RiskStore(Path(directory)/"audit.db")
            first={"trade_date":"2026-09-10","open":100,"high":103,"low":99,"close":101,"amount":None}
            response={"source_id":"test","fetched_at":"2026-09-10T16:00:00+08:00","rows":[first],"raw_payload":{"original":"first"}}
            store.ingest("sh000300",response)
            store.ingest("sh000300",dict(response,fetched_at="2026-09-11T16:00:00+08:00"))
            self.assertEqual(store.bars("sh000300")[0]["first_seen_at"],response["fetched_at"])
            store.ingest("sh000300",dict(response,rows=[dict(first,close=102)]))
            with store.connection() as connection:
                old=json.loads(connection.execute("SELECT payload FROM risk_bar_revisions").fetchone()[0])
                self.assertEqual(old["close"],101)
            for kind in ("published","backtest"):
                store.save_history("sh000300",[{"as_of":"2026-09-10","score":10}],kind)
                store.save_history("sh000300",[{"as_of":"2026-09-10","score":20}],kind)
            self.assertEqual(store.history("sh000300","published")[0]["score"],10)
            self.assertEqual(store.history("sh000300","backtest")[0]["score"],20)
            store.save_history("sh000300",[{"as_of":"2026-09-10","score":None}],"backtest")
            self.assertEqual(store.history("sh000300","backtest"),[])
            self.assertEqual(len(store.history("sh000300","published")),1)


class TestRiskApi(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="风险接口-")
        self.service=RiskService(Database(Path(self.temp.name)/"data.db"),logging.getLogger("risk-test"),fixture=True)
        app=FastAPI();app.include_router(router(self.service))
        self.client=TestClient(app)
        self.calendar_patch=patch("scripts.a_share_panic_index.risk_v4.service.expected_close_day",return_value="2026-09-11")
        self.calendar_patch.start()

    def tearDown(self):
        self.calendar_patch.stop();self.client.close();self.service.close();self.temp.cleanup()

    def seed(self,code,score=50,day="2026-09-11"):
        self.service.store.put("state:"+code,{"state":"ready","score":score,"as_of":day,"overheat":None,"components":{key:{"raw":1,"percentile":score} for key in ("shock","drawdown","downside")},"missing":["无成交额"]})

    def test_empty_start_and_bad_input_are_structured(self):
        response=self.client.get("/api/v2/risk/snapshot")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["state"],"insufficient_data")
        self.assertEqual(self.client.get("/api/v2/advice").json()["state"],"empty")
        self.assertEqual(self.client.get("/api/v2/portfolio").json()["cash"],0)
        self.assertEqual(self.client.get("/api/v2/risk/snapshot?symbol=../../secret").status_code,400)
        self.assertEqual(self.client.get("/api/v2/risk/history?limit=-1").status_code,422)
        self.assertEqual(self.client.post("/api/v2/risk/refresh",json={"symbols":[None]}).status_code,400)

    def test_market_requires_three_same_day_inputs_and_aux_has_no_weight(self):
        self.seed("sh000300",10);self.seed("sh000905",80)
        self.assertIsNone(self.service.snapshot()["score"])
        self.seed("sh000852",90,"2026-09-10")
        self.assertIsNone(self.service.snapshot()["score"])
        self.seed("sh000852",90)
        self.assertEqual(self.service.snapshot()["score"],80)
        self.service.store.put("auxiliary",{"state":"error","error":"辅助来源断网"})
        self.assertEqual(self.service.snapshot()["score"],80)

    def test_preview_csv_does_not_mutate_then_explicit_save(self):
        csv="基金代码,持有市值,估值日期\n013273,1000,2026-09-11\n"
        preview=self.client.post("/api/v2/portfolio/import-csv",json={"text":csv})
        self.assertTrue(preview.json()["valid"])
        self.assertEqual(self.service.portfolio()["lots"],[])
        saved=self.client.put("/api/v2/portfolio",json=preview.json()["portfolio"])
        self.assertEqual(saved.status_code,200)
        self.assertEqual(self.service.portfolio()["lots"][0]["code"],"013273")

    def test_failure_does_not_hide_existing_true_history_or_daily_record(self):
        self.service.store.save_history("market",[{"as_of":"2026-09-10","score":20}],"published")
        with patch.dict("os.environ",{"RISK_DISABLE_NETWORK":"1"}):
            failed=self.client.post("/api/v2/risk/refresh",json={})
        self.assertEqual(failed.status_code,400)
        self.assertEqual(self.client.get("/api/v2/risk/history?kind=published").json()["records"][0]["score"],20)

    def test_observation_and_stale_gates_hide_probabilities(self):
        self.seed("sh000300",55)
        latest={"state":"published","as_of":"2026-09-11","published":{"probabilities":{"20d_5pct":.2},"quantiles":{"20d_q10":-.02,"20d_q50":.01,"20d_q90":.05}},"reasons":[]}
        with patch.object(self.service,"_forecast",return_value=latest):
            self.assertEqual(self.service.snapshot("sh000300")["forecast"]["published"],{})
            with patch.object(self.service.store,"observation",return_value={"ready":True,"observed_days":20}):
                live=self.service.snapshot("sh000300")
                self.assertEqual(live["forecast"]["published"]["20"]["probabilities"]["5"],.2)
                self.seed("sh000300",55,"2026-09-10")
                stale=self.service.snapshot("sh000300")
                self.assertEqual(stale["state"],"stale")
                self.assertEqual(stale["forecast"]["published"],{})

    def test_unverifiable_old_model_cache_cannot_publish(self):
        for model in ({"latest":{"state":"published","published":{"probabilities":{"20d_5pct":.2}}}},
                      {"artifact":{"version":"4.0"}}):
            result=self.service._forecast("sh000300",model)
            self.assertEqual(result["state"],"unavailable")
            self.assertEqual(result["published"],{})

    def test_different_configuration_history_is_retained_but_not_spliced(self):
        self.service.store.save_history("market",[{"as_of":"2026-09-10","score":20}],"published")
        with self.service.store.connection() as con:
            con.execute("UPDATE risk_history SET payload=?",(json.dumps({"as_of":"2026-09-10","score":20,"configuration_sha256":"old"}),))
        self.assertEqual(self.service.store.history("market","published"),[])
        with self.service.store.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM risk_history").fetchone()[0],1)

    def test_interval_only_whitelist_never_fills_failed_event_with_zero(self):
        result=public_forecast({"state":"published","published":{"quantiles":{"20d_q10":-.05,"20d_q50":.01,"20d_q90":.1}}},"sh000300")
        self.assertNotIn("5",result["published"]["20"]["probabilities"])
        self.assertEqual(result["published"]["20"]["quantiles"]["q10"],-.05)

    def test_nav_period_return_never_becomes_a_daily_covariance_sample(self):
        bars=[{"trade_date":day,"close":100} for day in ("2026-09-07","2026-09-08","2026-09-09","2026-09-10")]
        series=[{"trade_date":"2026-09-09","period_start":"2026-09-07","return":.02,"segment":0},
                {"trade_date":"2026-09-10","period_start":"2026-09-09","return":.01,"segment":0}]
        daily=daily_return_filter(series,bars)
        self.assertEqual([row["trade_date"] for row in daily],["2026-09-10"])
        self.assertTrue(daily[0]["gap_before"])

    def test_revised_training_inputs_require_retraining(self):
        row={"trade_date":"2026-09-11","open":100,"high":103,"low":99,"close":101,"amount":None}
        self.service.store.ingest("sh000300",{"source_id":"test","rows":[row]})
        original=self.service.store.bars("sh000300")
        model={"artifact":{"version":"4.0"},"data_provenance":{"through":"2026-09-11","sha256":data_fingerprint(original)}}
        self.service.store.ingest("sh000300",{"source_id":"test","rows":[dict(row,close=102)]})
        result=self.service._forecast("sh000300",model)
        self.assertEqual(result["state"],"unavailable")
        self.assertIn("修订",result["reasons"][0])

    def test_intraday_retraining_does_not_mix_published_signal(self):
        old={"state":"published","as_of":"2026-09-11","published":{"probabilities":{"20d_5pct":.1}}}
        self.service.store.publish_forecast("sh000300",old)
        newer=dict(old,published={"probabilities":{"20d_5pct":.6}})
        value=self.service._signal("sh000300",{"state":"ready","as_of":"2026-09-11"},newer,{})
        self.assertFalse(value["eligible"])
        self.assertIn("不一致",value["reason"])

    def test_stale_execution_terms_only_allow_observation(self):
        self.service.update_portfolio({"cash":1000,"lots":[{"id":"a","code":"013273","market_value":500,"valuation_date":"2026-09-11"}]})
        self.service.store.put("fund:013273",{"metadata":{"symbol":"sh000300","verified":True},"history":{},
            "execution":{"redemption_verified":True,"redemption_open":True,"next_redemption_date":"2026-09-14","fetched_at":"2020-01-01T09:00:00+08:00"}})
        captured={}
        def inspect(portfolio,contexts,**kwargs):
            captured.update(contexts)
            return {"positions":[],"warnings":[]}
        with patch("scripts.a_share_panic_index.risk_v4.portfolio.build_advice",side_effect=inspect), patch.object(self.service,"_signal",return_value={"action":"review_exit","eligible":True}):
            self.service.advice()
        self.assertFalse(captured["013273"]["calendar_verified"])
        self.assertFalse(captured["013273"]["strategy"]["signal"]["eligible"])

    def test_mixed_lots_use_one_effective_manual_benchmark_context(self):
        plain={"id":"b","code":"013273","market_value":500,"valuation_date":"2026-09-11"}
        manual=dict(plain,id="a",metadata_verified=True,benchmark_symbol="sh000905",asset_class="equity",effective_date="2026-09-01")
        self.service.update_portfolio({"cash":1000,"lots":[manual,plain]})
        self.service.store.put("fund:013273",{"metadata":{"symbol":"sh000300","verified":True},"history":{}})
        captured={}
        def inspect(portfolio,contexts,**kwargs):
            captured.update(contexts)
            return {"positions":[],"warnings":[]}
        with patch("scripts.a_share_panic_index.risk_v4.portfolio.build_advice",side_effect=inspect):
            self.service.advice()
        self.assertEqual(captured["013273"]["symbol"],"sh000905")
        self.assertEqual(captured["013273"]["forecast"]["symbol"],"sh000905")


if __name__=="__main__":
    unittest.main()
