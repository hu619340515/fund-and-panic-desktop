"""新模型本地诊断与完整交付验收入口。"""
from __future__ import annotations
import multiprocessing
if __name__ == "__main__":multiprocessing.freeze_support()
import argparse
import json
import logging
import time
from pathlib import Path
from a_share_panic_index.database import Database
from a_share_panic_index.risk_v4.service import RiskService


def main():
    parser=argparse.ArgumentParser(description="本地市场风险V4诊断")
    parser.add_argument("action",choices=["refresh","train","snapshot","validation"])
    parser.add_argument("--database",required=True)
    parser.add_argument("--symbol",default="market")
    parser.add_argument("--fund-code",action="append",default=None,help="同时核验公开基金代码，可重复传入；不传持仓信息")
    parser.add_argument("--timeout",type=int,default=3600)
    args=parser.parse_args()
    db=Database(Path(args.database));logger=logging.getLogger("risk-v4-cli");logging.basicConfig(level=logging.INFO)
    service=RiskService(db,logger)
    try:
        if args.action in {"refresh","train"}:
            task=service.refresh(None if args.symbol=="market" else [args.symbol],args.fund_code) if args.action=="refresh" else service.train(None if args.symbol=="market" else [args.symbol])
            start=time.monotonic();last=None
            while time.monotonic()-start<args.timeout:
                task=next(j for j in service.store.jobs() if j["id"]==task["id"])
                status=(task["status"],task["message"],task["progress"])
                if status!=last:print(json.dumps({k:task[k] for k in ("status","message","progress","errors")},ensure_ascii=False),flush=True);last=status
                if task["status"] in {"completed","failed"}:break
                time.sleep(.5)
            else:raise TimeoutError("诊断任务超时")
            if task["status"]=="failed" or task["errors"]:raise RuntimeError(task["message"]+str(task["errors"]))
        value=service.validation(args.symbol) if args.action in {"train","validation"} else service.snapshot(args.symbol)
        print(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2))
    finally:service.close()

if __name__=="__main__":main()
