"""基金自身真实总回报与已核实基准的暴露核对；不生成基金价格预测。"""
from __future__ import annotations

import math
from datetime import date
import numpy as np


def check_exposure(fund_returns, benchmark_bars, *, as_of):
    benchmark = {}
    ordered = sorted((row for row in benchmark_bars if row["trade_date"]<=as_of), key=lambda row:row["trade_date"])
    benchmark_positions = {row["trade_date"]: index for index,row in enumerate(ordered)}
    for previous,current in zip(ordered,ordered[1:]):
        if not current.get("gap_before") and previous["close"]>0 and current["close"]>0:
            benchmark[current["trade_date"]]=(previous["trade_date"],current["close"]/previous["close"]-1)
    fund = {row["trade_date"]:(row["return"],row.get("segment")) for row in fund_returns
            if row.get("trade_date", "9999")<=as_of and isinstance(row.get("return"),(float,int))
            and math.isfinite(row["return"]) and not row.get("gap_before")
            and row.get("trade_date") in benchmark
            and row.get("period_start")==benchmark[row["trade_date"]][0]}
    dates=sorted(set(fund)&set(benchmark))[-252:]
    if len(dates)<252:
        return {"available":False,"stable":False,"observations":len(dates),"reason":"基金与基准共同真实日收益率不足252日，不能核实映射稳定性"}
    positions=[benchmark_positions[day] for day in dates]
    segments={fund[day][1] for day in dates}
    if positions[-1]-positions[0]!=251 or len(segments)!=1 or None in segments:
        return {"available":False,"stable":False,"observations":len(dates),"reason":"最近252个交易日含缺失净值或跨事件分段，不能核实映射稳定性"}
    if (date.fromisoformat(as_of)-date.fromisoformat(dates[-1])).days>7:
        return {"available":False,"stable":False,"observations":len(dates),"reason":"共同真实收益率最近日期距离决策日超过七个自然日"}
    x=np.array([benchmark[day][1] for day in dates]);y=np.array([fund[day][0] for day in dates])
    def fit(left,right):
        centered=left-left.mean();target=right-right.mean()
        variance=float(centered@centered)
        beta=float(centered@target)/(variance+1e-5)
        residual=target-beta*centered
        r2=1-float(residual@residual)/max(float(target@target),1e-12)
        return beta,r2
    beta,r2=fit(x,y);first,_=fit(x[:126],y[:126]);last,_=fit(x[126:],y[126:])
    stable=bool(beta>0 and r2>=.5 and abs(first-last)<=.35)
    return {"available":True,"stable":stable,"observations":252,"data_as_of":dates[-1],
            "beta":round(beta,6),"r_squared":round(r2,6),"earlier_beta":round(first,6),"recent_beta":round(last,6),
            "method":"demeaned_ridge_1e-5","reason":"仅核对基准参考关系，不等于基金自己的风险概率；稳定门槛为研究约束" if stable else "基金与所填基准关系偏弱或不稳定，关闭该映射下的操作建议"}
