"""后台任务与可取消的真实 spawn 子进程；退出时统一回收。"""
from __future__ import annotations
import multiprocessing as mp
import os
import threading
import time
from uuid import uuid4
from .store import timestamp


def _watch_parent(parent_pid):
    from ..worker_diagnostics import _pid_is_alive
    while _pid_is_alive(parent_pid):
        time.sleep(.5)
    os._exit(0)


def _worker(pipe, operation, args, parent_pid):
    threading.Thread(target=_watch_parent,args=(parent_pid,),daemon=True).start()
    try:
        if operation=="diagnostic":
            from pathlib import Path
            mode, path = args
            Path(path).write_text(str(os.getpid()),encoding="utf-8")
            if mode=="exit":os._exit(23)
            if mode=="timeout":time.sleep(120)
            if mode=="error":raise ValueError("测试数据源返回异常")
            value={"pid":os.getpid(),"mode":mode}
        elif operation=="history":
            from .providers import fetch_history
            value=fetch_history(*args)
        elif operation=="intraday":
            from .providers import fetch_intraday
            value=fetch_intraday()
        elif operation=="train":
            from .forecast import train_and_validate
            def progress(*details,**kwargs):
                pipe.send(("progress",{"details":details,"kwargs":kwargs}))
            value=train_and_validate(args[0],symbol=args[1],progress=progress)
        elif operation=="fund":
            from .nav import fetch_fund_history,fetch_fund_metadata
            from .execution import fetch_execution_terms
            history=fetch_fund_history(args[0]);metadata=fetch_fund_metadata(args[0])
            value={"version":"4.0","history":history,"metadata":metadata,
                   "execution":fetch_execution_terms(args[0],metadata)}
        elif operation=="auxiliary":
            from .providers import fetch_auxiliary
            value=fetch_auxiliary()
        else:
            raise ValueError("未知后台操作")
        pipe.send(("result",value))
    except BaseException as error:
        try:pipe.send(("error",{"type":type(error).__name__,"message":str(error)}))
        except (BrokenPipeError,EOFError,OSError):pass
    finally:
        pipe.close()


class JobManager:
    def __init__(self,store,logger):
        self.store=store;self.logger=logger
        self.stop_event=threading.Event();self.lock=threading.Lock();self.processes=set();self.threads=[]
        for job in self.store.jobs():
            if job["status"] in {"running","queued"}:
                job.update(status="failed",message="上次退出中断；已保留下载数据，可重新执行续传",completed_at=timestamp())
                self.store.job(job)

    def submit(self,kind,operation):
        with self.lock:
            for value in self.store.jobs():
                if value["kind"]==kind and value["status"] in {"running","queued"}:
                    return value
            job={"id":uuid4().hex,"kind":kind,"status":"queued","progress":0,"message":"任务已排队","errors":[],"started_at":timestamp(),"completed_at":None}
            self.store.job(job)
            def run():
                try:
                    self.update(job,status="running",message="任务执行中")
                    result=operation(job)
                    self.update(job,status="completed",progress=1,message="任务完成" if not job["errors"] else "已完成可用部分；失败项可重试",result=result,completed_at=timestamp())
                except Exception as error:
                    self.logger.exception("V4后台任务%s失败",kind)
                    self.update(job,status="failed",message=str(error),completed_at=timestamp())
            thread=threading.Thread(target=run,name="risk-"+kind,daemon=True)
            self.threads.append(thread);thread.start()
            return dict(job)

    def update(self,job,**changes):
        job.update(changes);self.store.job(job)

    def call(self,operation,*args,timeout=90,job=None):
        if self.stop_event.is_set():raise RuntimeError("客户端正在退出")
        ctx=mp.get_context("spawn");parent,child=ctx.Pipe(duplex=False)
        process=ctx.Process(target=_worker,args=(child,operation,args,os.getpid()),daemon=True)
        start=time.monotonic()
        with self.lock:
            if self.stop_event.is_set():
                parent.close();child.close();raise RuntimeError("客户端正在退出")
            try:
                process.start()
                self.processes.add(process)
            except BaseException:
                parent.close();child.close();process.close()
                raise
        child.close()
        try:
            while time.monotonic()-start<timeout:
                if self.stop_event.is_set():raise RuntimeError("任务已随客户端退出取消")
                if parent.poll(.2):
                    try:kind,value=parent.recv()
                    except EOFError:
                        process.join(timeout=.5)
                        raise RuntimeError(f"{operation}采集管道关闭；退出码{process.exitcode}")
                    if kind=="result":return value
                    if kind=="error":raise RuntimeError(f"{operation}：{value['type']}：{value['message']}")
                    if kind=="progress" and job:
                        self.update(job,message="训练进度："+str(value)[:300])
                if not process.is_alive():
                    if parent.poll(.1):continue
                    raise RuntimeError(f"{operation}子进程异常退出，退出码{process.exitcode}")
            raise TimeoutError(f"{operation}超过{timeout}秒，子进程已终止，可重试")
        finally:
            parent.close()
            process.join(timeout=.5)
            if process.is_alive():process.terminate()
            process.join(timeout=3)
            if process.is_alive():process.kill();process.join(timeout=2)
            with self.lock:self.processes.discard(process)
            self.logger.info("V4数据任务=%s exit=%s elapsed=%.2fs",operation,process.exitcode,time.monotonic()-start)
            process.close()

    def close(self):
        self.stop_event.set()
        with self.lock:
            for process in list(self.processes):
                if process.is_alive():process.terminate()
        for thread in self.threads:thread.join(timeout=3)
