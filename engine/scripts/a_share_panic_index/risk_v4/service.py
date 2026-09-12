"""V4 应用服务：历史与正式输出隔离，所有昂贵工作在后台执行。"""
from __future__ import annotations
import copy
import hashlib
import os
from datetime import datetime,timedelta
from statistics import median
from zoneinfo import ZoneInfo
from .store import RiskStore,timestamp,dump
from .providers import SYMBOLS,CORE_SYMBOLS,expected_close_day,_calendar
from .jobs import JobManager


def compact(value):
    """界面只传诊断摘要；逐日净值、网格明细与原响应留在本地审计库。"""
    if isinstance(value,dict):
        result={key:compact(item) for key,item in value.items()
                if key not in {"daily_returns","daily_nav","raw_payload","snapshot","raw_records","grid","exit_grid","change_percent_values"}}
        for key in ("missing_dates","non_session_rows","rejected","ohlc_rounding_repaired_dates","amount_missing_dates"):
            if isinstance(result.get(key),list) and len(result[key])>20:
                result[key+"_count"]=len(result[key]);result[key]=result[key][-20:]
        return result
    if isinstance(value,list):
        return [compact(item) for item in value]
    return value


def data_fingerprint(bars,through=None):
    fields=("trade_date","open","high","low","close","amount","gap_before")
    values=[{key:row.get(key) for key in fields} for row in bars if through is None or row["trade_date"]<=through]
    return hashlib.sha256(dump(values).encode("utf-8")).hexdigest()


def daily_return_filter(series,benchmark_bars):
    """只把同一基准相邻交易日之间的基金收益交给日频协方差。"""
    ordered=sorted(benchmark_bars,key=lambda row:row["trade_date"])
    previous_days={current["trade_date"]:previous["trade_date"] for previous,current in zip(ordered,ordered[1:]) if not current.get("gap_before")}
    result=[];last=None
    for row in series:
        day=row.get("trade_date")
        if row.get("period_start")!=previous_days.get(day) or row.get("period_start") is None or row.get("return") is None or row.get("gap_before"):
            last=None
            continue
        value=dict(row)
        value["gap_before"]=last is not None and value["period_start"]!=last
        if not result or last is None:
            value["gap_before"]=True
        result.append(value);last=day
    return result


def public_forecast(result,symbol):
    published=result.get("published") or {}
    probabilities=published.get("probabilities",{})
    quantiles=published.get("quantiles",{})
    rows={}
    if probabilities or quantiles:
        for horizon in (5,10,20,30,50):
            p={str(k):probabilities[f"{horizon}d_{k}pct"] for k in (3,5,10) if f"{horizon}d_{k}pct" in probabilities}
            q={f"q{k}":quantiles[f"{horizon}d_q{k}"] for k in (10,50,90) if f"{horizon}d_q{k}" in quantiles}
            if p or q:
                rows[str(horizon)]={"probabilities":p,"quantiles":q}
    return {"symbol":symbol,"state":result.get("state","unavailable"),"as_of":result.get("as_of"),"published":rows,"reasons":result.get("reasons",["模型尚未训练或未通过发布门槛"])}


class RiskService:
    def __init__(self,database,logger,*,fixture=False):
        self.store=RiskStore(database.path);self.logger=logger;self.fixture=fixture
        self.jobs=JobManager(self.store,logger)

    def _forecast(self, symbol, trained=None):
        trained = trained if trained is not None else self.store.get("model:"+symbol, {})
        result = {"state":"unavailable","published":{},"reasons":["缺少可核验的模型参数，请重新训练和验证"]}
        if trained.get("artifact"):
            from .forecast import predict
            try:
                bars=self.store.bars(symbol)
                provenance=trained.get("data_provenance",{})
                if not provenance.get("through") or not provenance.get("sha256"):
                    return {"state":"unavailable","published":{},"reasons":["训练输入缺少来源指纹，请重新训练和验证"]}
                if data_fingerprint(bars,provenance["through"])!=provenance["sha256"]:
                    return {"state":"unavailable","published":{},"reasons":["训练输入历史已有修订，请重新训练和验证"]}
                result = predict(bars, trained["artifact"])
            except Exception as error:
                result = {"state":"unavailable", "reasons":[str(error)]}
        return copy.deepcopy(result)

    def _signal(self, symbol, state, forecast, trained, *, has_position=False):
        from .strategy import evaluate_signal
        observation = self.store.observation()
        forecasts = self.store.published_forecasts(symbol, 5)
        existing=[row for row in forecasts if row.get("as_of")==forecast.get("as_of")]
        if existing and existing[-1].get("published") != forecast.get("published"):
            return {"action":"observe","eligible":False,"reason":"当日重训或数据修订使预测与已发布记录不一致，暂停操作建议至下一交易日"}
        states_by_date = {row["as_of"]:row for row in self.store.history(symbol,"published",5)}
        closes_by_date = {row["trade_date"]:row["close"] for row in self.store.bars(symbol)}
        aligned = [row for row in forecasts if row["as_of"] in states_by_date and row["as_of"] in closes_by_date]
        context = {"strategy_validation":trained.get("strategy",{}),
                   "live_observation_days":observation["observed_days"],
                   "style":self.portfolio()["profile"], "has_position":has_position,
                   "recent_forecasts":aligned,
                   "recent_states":[states_by_date[row["as_of"]] for row in aligned],
                   "recent_closes":[closes_by_date[row["as_of"]] for row in aligned],
                   "close":closes_by_date.get(state.get("as_of"))}
        if context["close"] is None:
            return {"action":"observe","eligible":False,"reason":"缺少当前状态日期的真实收盘价"}
        signal = evaluate_signal(state,forecast,context)
        if not observation["ready"] or state.get("state") != "ready":
            return {"action":"observe","eligible":False,"reason":"先核实数据并完成模型验证及20个交易日观察","details":signal}
        return signal

    def close(self):self.jobs.close()

    def validate_symbol(self,symbol):
        if symbol!="market" and symbol not in SYMBOLS:
            raise ValueError("未登记的市场标的")
        return symbol

    def snapshot(self,symbol="market"):
        self.validate_symbol(symbol)
        rows=[]
        for code,meta in SYMBOLS.items():
            state=self.store.get("state:"+code,{})
            rows.append({"symbol":code,"name":meta["name"],"score":state.get("score"),"state":state.get("state","insufficient_data"),"as_of":state.get("as_of")})
        if symbol=="market":
            value=self._market_state()
            forecast_symbol="sh000300"
        else:
            value=self.store.get("state:"+symbol,{"state":"insufficient_data","as_of":None,"score":None,"components":{},"overheat":None,"explanation":["请下载真实历史日线"],"missing":["历史日线"]})
            forecast_symbol=symbol
        value=copy.deepcopy(value)
        expected=expected_close_day(forecast_symbol)
        if value.get("score") is not None and value.get("as_of")!=expected:
            value["state"]="stale"
            value.setdefault("missing",[]).append("最新完整收盘数据尚未取得："+expected)
        trained=self.store.get("model:"+forecast_symbol,{})
        latest=self._forecast(forecast_symbol,trained)
        forecast=public_forecast(latest,forecast_symbol)
        observation=self.store.observation()
        if not observation["ready"]:
            forecast["published"]={}
            forecast["state"]="research_only" if trained else "unavailable"
            forecast.setdefault("reasons",[]).append(f"实盘观察{observation['observed_days']}/20个交易日，操作性建议尚未启用")
        if value.get("state") != "ready":
            forecast["published"]={}
            forecast["state"]="unavailable"
            forecast.setdefault("reasons",[]).append("收盘状态数据无效或过期，不发布新的风险预测")
        signal_state=value if symbol!="market" else self.store.get("state:sh000300",{})
        signal=self._signal(forecast_symbol,signal_state,latest,trained)
        signal["symbol"]=forecast_symbol
        opportunity={"state":"waiting","label":"等待","reasons":[signal.get("reason","等待验证")]}
        if value.get("score") is not None and value["score"]>=75:
            opportunity.update(state="observe",label="观察",reasons=["压力处于历史高位；仍须价格企稳、收益覆盖成本及独立验证通过",signal.get("reason","")])
        if signal.get("eligible") and signal.get("action")=="review_entry":
            opportunity.update(state="eligible",label="可分批介入（待复核）",reasons=[signal["reason"],"实际基金及总仓位仍须通过费用、确认时间和组合约束"])
        heat=value.get("overheat")
        heat_signal={"state":"unavailable" if heat is None else "observe","label":"暂无法判断" if heat is None else "正常观察",
                     "reasons":["短期过热反映涨幅、均线偏离和成交活跃，不代表估值泡沫"]}
        if heat is not None and heat>=75:
            heat_signal.update(state="avoid_chasing",label="不追涨",reasons=["短期过热较高，减配仍需趋势、未来风险和成本验证",signal.get("reason","")])
        if signal.get("eligible") and signal.get("action")=="review_exit":
            heat_signal.update(state="eligible",label="考虑减配（待复核）",reasons=[signal["reason"]])
        value.update(model_version="4.0",symbol=symbol,symbols=rows,forecast=forecast,signal=signal,observation=observation,
                     opportunity=opportunity,heat_signal=heat_signal,
                     data_quality=compact(self.store.get("source:"+forecast_symbol,{})),intraday=self.store.get("intraday",{}),auxiliary=compact(self.store.get("auxiliary",{})),jobs=self.job_status())
        value["forecast"]["notice"]="市场概览风险以沪深300为参照；不平均不同指数的风险概率" if symbol=="market" else ""
        value["intraday"]={k:v for k,v in value["intraday"].items() if k!="raw_payload"}
        return compact(value)

    def _market_state(self):
        states=[self.store.get("state:"+code,{}) for code in CORE_SYMBOLS]
        if any(v.get("score") is None for v in states) or len({v.get("as_of") for v in states})!=1:
            return {"state":"insufficient_data","score":None,"as_of":None,"components":{},"overheat":None,"explanation":["市场概览要求三种风格同日完整评分，不以可用子集替代"],"missing":[SYMBOLS[c]["name"] for c,s in zip(CORE_SYMBOLS,states) if s.get("score") is None] or ["三种风格数据日期不一致"]}
        components={}
        for key in ("shock","drawdown","downside"):
            vals=[s.get("components",{}).get(key,{}) for s in states]
            components[key]={"raw":None,"percentile":median(v["percentile"] for v in vals)}
        hot=[s.get("overheat") for s in states]
        return {"state":"ready","score":median(s["score"] for s in states),"as_of":states[0]["as_of"],"components":components,
                "overheat":median(hot) if all(v is not None for v in hot) else None,"explanation":["沪深300、中证500、中证1000状态分的中位数；不是下跌概率。概览分项为各风格该项的中位数，不作为总分加总贡献。"],"missing":[SYMBOLS[c]["name"]+"："+reason for c,s in zip(CORE_SYMBOLS,states) for reason in s.get("missing",[])],"model_version":"4.0"}

    def history(self,symbol="market",kind="backtest",limit=252):
        self.validate_symbol(symbol)
        if kind not in {"backtest","published"}:raise ValueError("未知历史类型")
        records=self.store.history(symbol,kind,limit)
        positions={row["trade_date"]:i for i,row in enumerate(self.store.bars("sh000300" if symbol=="market" else symbol))}
        previous=None
        for row in records:
            current=positions.get(row["as_of"])
            row["break_before"]=previous is not None and (current is None or current!=previous+1)
            previous=current
        return {"symbol":symbol,"kind":kind,"records":records,"missing":[],"notice":"回算仅用完整真实行情；严格发布时间不可核实部分不计为时间外验证通过" if kind=="backtest" else "实际在对应交易日发布的记录，之后回算不覆盖"}

    def validation(self,symbol="market"):
        self.validate_symbol(symbol)
        code="sh000300" if symbol=="market" else symbol
        model=self.store.get("model:"+code,{})
        value=copy.deepcopy(model.get("validation",{"status":"not_trained","publishable":False,"reasons":["请先下载历史并执行训练与验证"]}))
        value.update(symbol=code,strategy=model.get("strategy",{}),observation=self.store.observation())
        return compact(value)

    def job_status(self):
        return [compact({key:value for key,value in job.items() if key!="result"}) for job in self.store.jobs()]

    def portfolio(self):
        value=self.store.get("portfolio")
        return value if value is not None else {"version":1,"cash":0,"profile":"balanced","lots":[],"constraints":{"equity_cap":.8,"vol_target":.1,"single_fund_cap":.2,"industry_cap":.35,"daily_change_cap":.1,"no_trade_band":.05}}

    def update_portfolio(self,value):
        from .portfolio import validate_portfolio
        result=validate_portfolio(value)
        if not result.get("valid"):
            raise ValueError(str(result.get("errors")))
        self.store.put("portfolio",result["portfolio"])
        return result["portfolio"]

    def import_csv(self,text):
        from .portfolio import import_csv
        if len(text)>2_000_000:raise ValueError("CSV不得超过2MB")
        return import_csv(text)

    def advice(self):
        from .portfolio import build_advice
        from .exposure import check_exposure
        portfolio=self.portfolio();contexts={}
        if not portfolio["lots"] and not portfolio["cash"]:
            return {"state":"empty","as_of":None,"profile":portfolio["profile"],"summary":{"total_assets":0,"cash":0,"fund_count":0},"items":[],"positions":[],"reasons":["请录入持仓与现金后查看组合建议"],"constraints":portfolio["constraints"]}
        for code in dict.fromkeys(lot["code"] for lot in portfolio["lots"]):
            today=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            lots=[lot for lot in portfolio["lots"] if lot["code"]==code]
            manual=[lot for lot in lots if lot.get("metadata_verified") and lot.get("effective_date", "9999")<=today]
            lot=(manual or lots)[0]
            data=self.store.get("fund:"+code,{})
            history=data.get("history",{});meta=data.get("metadata",{})
            terms=data.get("execution",{})
            nav=[row for row in history.get("history",[]) if row["trade_date"]<=today]
            benchmark=meta.get("symbol") if meta.get("verified") else None
            if lot.get("metadata_verified") and lot.get("effective_date", "9999")<=today:
                benchmark=lot.get("benchmark_symbol")
            state=self.store.get("state:"+benchmark,{}) if benchmark else {}
            model=self.store.get("model:"+benchmark,{}) if benchmark else {}
            if benchmark in SYMBOLS and state.get("as_of")!=expected_close_day(benchmark):
                state=dict(state,state="stale")
            forecast=self._forecast(benchmark,model) if benchmark in SYMBOLS else {}
            signal=self._signal(benchmark,state,forecast,model,has_position=True) if benchmark in SYMBOLS else {}
            returns=history.get("total_return",{}).get("series",[]) if history.get("completeness",{}).get("total_return_verified") else []
            returns=[row for row in returns if row.get("trade_date", "9999")<=today]
            exposure=check_exposure(returns,self.store.bars(benchmark),as_of=today) if benchmark in SYMBOLS else {"available":False,"stable":False,"reason":"基准尚未核实或未登记"}
            if not exposure.get("stable"):
                signal=dict(signal,eligible=False,reason=exposure["reason"])
            direction="subscription" if signal.get("action")=="review_entry" else "redemption"
            direction_open=terms.get(direction+"_open") is True
            try:
                terms_day=datetime.fromisoformat(terms.get("fetched_at","").replace("Z","+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            except (ValueError,TypeError):
                terms_day=None
            next_day=terms.get("next_"+direction+"_date")
            calendar_verified=bool(terms.get(direction+"_verified") and direction_open and terms_day==today and next_day and next_day>=today)
            if not calendar_verified:
                signal=dict(signal,eligible=False,reason=terms.get("reason","申赎状态与提交日历尚未核实"))
            # 组合协方差基于基金自身收益；国内基金并不因为主动基准未核实就
            # 失去可核验日频收益。QDII 无已核实海外基准日历时保留缺项。
            fund_type=str(meta.get("fund_type", ""))
            if returns and "QDII" not in fund_type.upper() and any(kind in fund_type for kind in ("股票","混合","债券","货币","指数")):
                first=returns[0].get("period_start") or returns[0]["trade_date"]
                cal=_calendar("sh000300",first,returns[-1]["trade_date"])
                calendar_bars=[{"trade_date":day.date().isoformat()} for day in cal.sessions]
            else:
                calendar_bars=self.store.bars(benchmark) if benchmark in SYMBOLS else []
            daily_returns=daily_return_filter(returns,calendar_bars)
            contexts[code]={"nav":nav[-1].get("unit_nav") if nav else None,"nav_date":nav[-1].get("trade_date") if nav else None,
                            "returns":daily_returns,"name":meta.get("name"),"symbol":benchmark,"industry":meta.get("industry"),"state":state.get("state"),
                            "forecast":dict(forecast,symbol=benchmark,validation=model.get("validation",{})),"strategy":dict(model.get("strategy",{}),signal=signal),"fees_verified":all(v.get("fee_buy") is not None and v.get("fee_sell") is not None for v in lots),"calendar_verified":calendar_verified,
                            "observation_ready":self.store.observation()["ready"],"asset_class":lot.get("asset_class") if manual else meta.get("asset_class"),"exposure":exposure,"execution":terms}
        value=build_advice(portfolio,contexts,as_of=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
        value.update(items=value.get("positions",[]),reasons=value.get("warnings",[]))
        value["fund_quality"]=[{"code":code,"benchmark":context.get("symbol"),"nav_date":context.get("nav_date"),"exposure":context.get("exposure"),"execution":compact(context.get("execution",{}))} for code,context in contexts.items()]
        return value

    def refresh(self,symbols=None,fund_codes=None):
        selected=list(dict.fromkeys(symbols or SYMBOLS))
        if "market" in selected:selected=list(CORE_SYMBOLS)
        for code in selected:self.validate_symbol(code)
        if fund_codes is not None:
            if not isinstance(fund_codes,list) or len(fund_codes)>200 or any(not isinstance(code,str) or len(code)!=6 or not code.isascii() or not code.isdigit() for code in fund_codes):
                raise ValueError("基金清单须为最多200个六位代码")
            self.store.put("watch_funds",list(dict.fromkeys(fund_codes)))
        if os.environ.get("RISK_DISABLE_NETWORK")=="1":raise ValueError("当前离线验收模式禁止网络采集")
        def execute(job):
            for i,symbol in enumerate(selected):
                self.jobs.update(job,progress=i/max(1,len(selected)),message="下载并审计 "+SYMBOLS[symbol]["name"])
                try:
                    end=expected_close_day(symbol)
                    old=self.store.bars(symbol)
                    source=self.store.get("source:"+symbol,{})
                    # 按失败日期重试；首次全取，之后只重叠最近30自然日核对修订。
                    start=None
                    needs_amount = sum(row.get("amount") is not None for row in old) < 816
                    if old and not source.get("quality",{}).get("missing_dates") and not needs_amount:
                        start=(datetime.fromisoformat(old[-1]["trade_date"])-timedelta(days=30)).date().isoformat()
                    response=self.jobs.call("history",symbol,start,end,timeout=100,job=job)
                    self.store.ingest(symbol,response)
                    self._recompute(symbol)
                except Exception as error:
                    job["errors"].append({"symbol":symbol,"error":str(error)})
                    self.store.put("error:"+symbol,{"message":str(error),"at":timestamp()})
            self._recompute_market()
            try:self.store.put("intraday",self.jobs.call("intraday",timeout=20))
            except Exception as error:self.store.put("intraday",{"rows":[],"error":str(error)})
            try:self.store.put("auxiliary",self.jobs.call("auxiliary",timeout=110))
            except Exception as error:self.store.put("auxiliary",{"state":"error","error":str(error)})
            codes=list(dict.fromkeys(self.store.get("watch_funds",[])+[lot["code"] for lot in self.portfolio()["lots"]]))
            for code in codes:
                self.jobs.update(job,message="获取基金真实净值及基准："+code)
                try:
                    existing=self.store.get("fund:"+code,{})
                    old_rows=existing.get("history",{}).get("history",[])
                    terms_date=str(existing.get("execution",{}).get("fetched_at",""))[:10]
                    if existing.get("version")=="4.0" and old_rows and old_rows[-1]["trade_date"]>=expected_close_day("sh000300") and terms_date==datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
                        continue
                    data=self.jobs.call("fund",code,timeout=90)
                    if not data.get("history",{}).get("available"):
                        raise ValueError("；".join(data.get("history",{}).get("diagnostics",["基金真实净值不可用"])))
                    self.store.record_raw("fund:"+code,"public_fund_nav_and_metadata",data)
                    self.store.put("fund:"+code,data)
                except Exception as error:job["errors"].append({"fund":code,"error":str(error)})
            return {"symbols":selected,"funds":codes}
        return self.jobs.submit("refresh",execute)

    def _recompute(self,symbol):
        from .core import compute_history
        bars=self.store.bars(symbol)
        records=compute_history(bars,limit=max(252,len(bars)))
        state=records[-1] if records else {}
        if state.get("score") is None:
            previous=[row for row in records if row.get("score") is not None]
            if previous:
                state=dict(previous[-1],state="stale",missing=state.get("missing",[])+["最新收盘无法计算，显示最后有效值"],latest_attempt_as_of=state.get("as_of"))
        self.store.put("state:"+symbol,state)
        self.store.save_history(symbol,records,"backtest")
        today=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if state.get("as_of")==today and today==expected_close_day(symbol) and state.get("score") is not None and not self.fixture:
            self.store.save_history(symbol,[dict(state,published_at=timestamp())],"published")
            if self.store.observation()["ready"]:
                self.store.publish_forecast(symbol,self._forecast(symbol))

    def _recompute_market(self):
        histories={code:{v["as_of"]:v for v in self.store.history(code,"backtest",5000)} for code in CORE_SYMBOLS}
        days=set.intersection(*(set(v) for v in histories.values())) if histories else set()
        records=[]
        for day in sorted(days):
            values=[histories[c][day] for c in CORE_SYMBOLS]
            if all(v.get("score") is not None for v in values):
                records.append({"as_of":day,"score":median(v["score"] for v in values),"state":"ready","model_version":"4.0"})
        self.store.save_history("market",records,"backtest")
        state=self._market_state();today=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if not self.fixture and state.get("as_of")==today and today==expected_close_day("sh000300") and state.get("score") is not None:
            self.store.save_history("market",[dict(state,published_at=timestamp())],"published")
            self.store.observe(today,{"observed_at":timestamp(),"data_as_of":today,"score":state["score"]})

    def train(self,symbols=None):
        selected=list(dict.fromkeys(symbols or [code for code in SYMBOLS if self.store.bars(code)] or CORE_SYMBOLS))
        selected=["sh000300" if code=="market" else code for code in selected]
        for code in selected:self.validate_symbol(code)
        def execute(job):
            results={}
            for i,symbol in enumerate(selected):
                self.jobs.update(job,progress=i/max(1,len(selected)),message="训练与时间外验证："+SYMBOLS[symbol]["name"])
                try:
                    bars=self.store.bars(symbol)
                    result=self.jobs.call("train",bars,symbol,timeout=1200,job=job)
                    result["data_provenance"]={"through":bars[-1]["trade_date"],"sha256":data_fingerprint(bars)} if bars else {}
                    from .validation import backtest_strategy
                    result["strategy"]=backtest_strategy(bars,self.store.history(symbol,"backtest",10000),result.get("oos_predictions",[]))
                    self.store.put("model:"+symbol,result)
                    results[symbol]=result["validation"]
                    reports=self.store.path.parent.parent/"reports";reports.mkdir(exist_ok=True)
                    from .store import dump
                    (reports/("risk-validation-"+symbol+".json")).write_text(dump({"symbol":symbol,"validation":result["validation"],"strategy":result["strategy"],"model_artifact":result.get("artifact"),"observation":self.store.observation()}),encoding="utf-8")
                except Exception as error:job["errors"].append({"symbol":symbol,"error":str(error)})
            return results
        return self.jobs.submit("train",execute)
