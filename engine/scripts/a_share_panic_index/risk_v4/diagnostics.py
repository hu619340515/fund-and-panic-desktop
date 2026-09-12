"""与真实 EXE 共用的 V4 多进程自检；合成数据只验证程序链路。"""
from __future__ import annotations

import logging
import math
import tempfile
from datetime import date, timedelta
from pathlib import Path

from .jobs import JobManager
from .store import RiskStore
from ..worker_diagnostics import _pid_is_alive


def run_diagnostics():
    cases = {}
    from py_mini_racer import MiniRacer
    result = MiniRacer().eval("6 * 7")
    if result != 42:
        raise AssertionError("冻结版 PyMiniRacer 无法执行 JavaScript")
    cases["js_runtime"] = {"value": result}
    with tempfile.TemporaryDirectory(prefix="风险引擎多进程-") as directory:
        root = Path(directory)
        manager = JobManager(RiskStore(root / "测试.db"), logging.getLogger("risk-self-test"))
        pids = []
        try:
            for mode in ("success", "error", "exit", "timeout"):
                path = root / (mode + ".pid")
                try:
                    result = manager.call("diagnostic", mode, str(path), timeout=4)
                    if mode != "success":
                        raise AssertionError(mode + "没有按预期失败")
                    cases[mode] = result
                except (RuntimeError, TimeoutError) as error:
                    expected = {"error":"测试数据源返回异常", "exit":"23", "timeout":"超过"}
                    if mode not in expected or expected[mode] not in str(error):
                        raise
                    cases[mode] = {"error":str(error)}
                if not path.exists():
                    raise AssertionError(mode + "未经过实际子进程")
                pids.append(int(path.read_text(encoding="utf-8")))
            rows = []
            day = date(2010,1,4)
            price = 100.0
            for i in range(2050):
                while day.weekday() >= 5:
                    day += timedelta(days=1)
                price *= math.exp(.001 + .021 * math.sin(i*.137) + .008 * math.cos(i*.51))
                rows.append({"trade_date":day.isoformat(),"open":price,"high":price*1.01,
                             "low":price*.99,"close":price,"amount":1e8*(1.2+.2*math.sin(i)),
                             "pit_verified":False,"source_id":"synthetic_test"})
                day += timedelta(days=1)
            trained = manager.call("train", rows, "synthetic_test", timeout=180)
            if not trained.get("artifact") or not trained.get("oos_predictions"):
                raise AssertionError("EXE未完成真实训练、校准和时间外回放")
            if trained["latest"].get("published"):
                raise AssertionError("合成测试数据不允许通过实盘发布门槛")
            cases["training"] = {"oos_samples":len(trained["oos_predictions"]),
                                  "published":False,"data_type":"synthetic_test_only"}
        finally:
            manager.close()
        residual = [pid for pid in pids if _pid_is_alive(pid)]
        if residual:
            raise AssertionError("残留子进程："+str(residual))
    return {"ok":True,"start_method":"spawn","cases":cases,"residual_pids":[]}
