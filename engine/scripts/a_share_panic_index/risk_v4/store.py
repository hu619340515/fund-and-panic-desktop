"""V4 独立数据、原始响应、模型与发布记录；不读取旧评分表。"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .spec import CONFIG_SHA256

RISK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS risk_raw_responses(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source_id TEXT NOT NULL, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_bars(symbol TEXT NOT NULL, trade_date TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(symbol,trade_date));
CREATE TABLE IF NOT EXISTS risk_bar_revisions(symbol TEXT NOT NULL, trade_date TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, replaced_at TEXT NOT NULL, PRIMARY KEY(symbol,trade_date,revision));
CREATE TABLE IF NOT EXISTS risk_values(key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_history(symbol TEXT NOT NULL, trade_date TEXT NOT NULL, model_version TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('backtest','published')), payload TEXT NOT NULL, PRIMARY KEY(symbol,trade_date,model_version,kind));
CREATE TABLE IF NOT EXISTS risk_jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_observation(trade_date TEXT NOT NULL, model_version TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(trade_date,model_version));
CREATE TABLE IF NOT EXISTS risk_forecast_publications(symbol TEXT NOT NULL, trade_date TEXT NOT NULL, model_version TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol,trade_date,model_version));
"""

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

class RiskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript(RISK_SCHEMA_SQL)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, key, default=None):
        with self.connection() as con:
            row = con.execute("SELECT payload FROM risk_values WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connection() as con:
            con.execute("INSERT OR REPLACE INTO risk_values VALUES (?,?,?)", (key,dump(value),timestamp()))

    def record_raw(self,symbol,source,payload):
        fetched=timestamp();raw_text=dump(payload)
        identity=hashlib.sha256((symbol+source+fetched+raw_text).encode("utf-8")).hexdigest()
        with self.connection() as con:
            con.execute("INSERT OR IGNORE INTO risk_raw_responses VALUES (?,?,?,?,?)",(identity,symbol,source,fetched,raw_text))
        return identity

    def ingest(self, symbol, response):
        # 原响应与规范数据一同落盘；每次修订保留旧版本，不能覆盖审计证据。
        raw = response.get("raw_payload", response)
        raw_text = dump(raw)
        source = response["source_id"]
        fetched = response.get("fetched_at", timestamp())
        identity = hashlib.sha256((symbol+source+fetched+raw_text).encode("utf-8")).hexdigest()
        rows = response["rows"]
        with self.connection() as con:
            con.execute("INSERT OR IGNORE INTO risk_raw_responses VALUES (?,?,?,?,?)", (identity,symbol,source,fetched,raw_text))
            for incoming in rows:
                value = dict(incoming, source_id=source, fetched_at=fetched, raw_response_id=identity)
                if value.get("amount") is not None:
                    value.update(amount_source_id=source,amount_unit=response.get("amount_unit"),amount_raw_response_id=identity)
                previous = con.execute("SELECT payload,revision FROM risk_bars WHERE symbol=? AND trade_date=?", (symbol,value["trade_date"])).fetchone()
                revision = previous[1] if previous else 1
                if previous:
                    old = json.loads(previous[0])
                    # 历史同日已核实的成交额可独立保留；保留原来源/原响应标识，不能
                    # 让今日价格备用源的一项缺失抹掉既存证据或冒充今日新抓取的金额。
                    old_amount_source=old.get("amount_source_id",old.get("source_id"))
                    if value.get("amount") is None and old.get("amount") is not None and old_amount_source in {"csindex","eastmoney"}:
                        value.update(amount=old["amount"],amount_source_id=old_amount_source,
                                     amount_unit=old.get("amount_unit","CNY" if not symbol.startswith("hk") else "HKD"),
                                     amount_raw_response_id=old.get("amount_raw_response_id",old.get("raw_response_id")))
                    changed = any(old.get(k) != value.get(k) for k in ("open","high","low","close","amount","source_id"))
                    if not changed:
                        # 第一次实际看到数据的时间不得被后续抓取改写。
                        value["first_seen_at"] = old.get("first_seen_at", old.get("fetched_at"))
                    else:
                        con.execute("INSERT OR IGNORE INTO risk_bar_revisions VALUES (?,?,?,?,?)",(symbol,value["trade_date"],revision,previous[0],timestamp()))
                        revision += 1
                        value["first_seen_at"] = fetched
                else:
                    value["first_seen_at"] = fetched
                value["revision"] = revision
                con.execute("INSERT OR REPLACE INTO risk_bars VALUES (?,?,?,?)",(symbol,value["trade_date"],dump(value),revision))
        all_rows=self.bars(symbol)
        self.put("source:"+symbol, {k:v for k,v in response.items() if k not in {"rows","raw_payload"}} | {
            "count":len(all_rows),"response_count":len(rows),"raw_response_id":identity,
            "source_ids":sorted({row["source_id"] for row in all_rows}),
            "amount_coverage":sum(row.get("amount") is not None for row in all_rows)/len(all_rows) if all_rows else 0})

    def bars(self, symbol, through=None):
        with self.connection() as con:
            sql = "SELECT payload FROM risk_bars WHERE symbol=?"
            args = [symbol]
            if through:
                sql += " AND trade_date<=?"; args.append(through)
            records = con.execute(sql+" ORDER BY trade_date",args).fetchall()
        return [json.loads(r[0]) for r in records]

    def save_history(self, symbol, records, kind="backtest"):
        with self.connection() as con:
            for item in records:
                day = item.get("as_of")
                if day and kind == "backtest" and item.get("score") is None:
                    con.execute("DELETE FROM risk_history WHERE symbol=? AND trade_date=? AND model_version='4.0' AND kind='backtest'", (symbol, day))
                if not day or item.get("score") is None:
                    continue
                value = dict(item,record_kind=kind,model_version="4.0",configuration_sha256=CONFIG_SHA256)
                # 实际发布值不可被后续修订/回算覆盖。
                verb = "INSERT OR IGNORE" if kind == "published" else "INSERT OR REPLACE"
                con.execute(verb+" INTO risk_history VALUES (?,?,?,?,?)", (symbol,day,"4.0",kind,dump(value)))

    def history(self, symbol, kind="backtest", limit=252):
        with self.connection() as con:
            rows = con.execute("SELECT payload FROM risk_history WHERE symbol=? AND model_version='4.0' AND kind=? ORDER BY trade_date DESC LIMIT ?",(symbol,kind,limit)).fetchall()
        return [value for row in reversed(rows) if (value:=json.loads(row[0])).get("configuration_sha256")==CONFIG_SHA256]

    def publish_forecast(self, symbol, value):
        if not value.get("as_of") or value.get("state") != "published":
            return
        with self.connection() as con:
            con.execute("INSERT OR IGNORE INTO risk_forecast_publications VALUES (?,?,'4.0',?)",
                        (symbol, value["as_of"], dump(dict(value, published_at=timestamp(),configuration_sha256=CONFIG_SHA256))))

    def published_forecasts(self, symbol, limit=5):
        with self.connection() as con:
            rows = con.execute("SELECT payload FROM risk_forecast_publications WHERE symbol=? AND model_version='4.0' ORDER BY trade_date DESC LIMIT ?", (symbol, limit)).fetchall()
        return [value for row in reversed(rows) if (value:=json.loads(row[0])).get("configuration_sha256")==CONFIG_SHA256]

    def job(self, value):
        with self.connection() as con:
            con.execute("INSERT OR REPLACE INTO risk_jobs VALUES (?,?,?)",(value["id"],dump(value),timestamp()))

    def jobs(self):
        with self.connection() as con:
            rows=con.execute("SELECT payload FROM risk_jobs ORDER BY updated_at DESC LIMIT 30").fetchall()
        return [json.loads(row[0]) for row in rows]

    def observe(self, day, value):
        with self.connection() as con:
            con.execute("INSERT OR IGNORE INTO risk_observation VALUES (?,'4.0',?)",(day,dump(dict(value,configuration_sha256=CONFIG_SHA256))))

    def observation(self):
        with self.connection() as con:
            rows=con.execute("SELECT trade_date,payload FROM risk_observation WHERE model_version='4.0' ORDER BY trade_date").fetchall()
        rows=[row for row in rows if json.loads(row[1]).get("configuration_sha256")==CONFIG_SHA256]
        return {"required_days":20,"observed_days":len(rows),"ready":len(rows)>=20,"dates":[r[0] for r in rows],"release_stage":"observation" if len(rows)<20 else "eligible_for_review"}
