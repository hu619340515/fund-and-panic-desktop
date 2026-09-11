"""验证打包引擎的 spawn 子进程、异常退出和硬超时清理。"""

from __future__ import annotations

import ctypes
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .providers.base import ProviderError, ProviderTimeout, run_with_hard_timeout


class WorkerDiagnosticsError(RuntimeError):
    """多进程诊断没有满足预期。"""


def _success_worker(value: str) -> dict[str, Any]:
    return {"pid": os.getpid(), "value": value}


def _abnormal_exit_worker(pid_path: str) -> None:
    Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
    os._exit(23)


def _timeout_worker(pid_path: str, sleep_seconds: float) -> None:
    Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(sleep_seconds)


def _read_pid(path: Path, case_name: str) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise WorkerDiagnosticsError(f"{case_name} worker 未写入有效 PID") from error


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def run_worker_diagnostics(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """通过生产硬超时函数运行三类真实 spawn worker，并验证全部已回收。"""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")

    cases: dict[str, dict[str, Any]] = {}
    worker_pids: list[int] = []
    with tempfile.TemporaryDirectory(prefix="panic-worker-diagnostics-") as directory:
        root = Path(directory)

        started = time.monotonic()
        success = run_with_hard_timeout(
            _success_worker,
            ("spawn-ok",),
            timeout_seconds,
        )
        if not isinstance(success, dict) or success.get("value") != "spawn-ok":
            raise WorkerDiagnosticsError(f"成功 worker 返回值异常: {success!r}")
        success_pid = int(success["pid"])
        worker_pids.append(success_pid)
        cases["success"] = {
            "pid": success_pid,
            "value": success["value"],
            "elapsed_seconds": _elapsed(started),
        }

        abnormal_pid_path = root / "abnormal-exit.pid"
        started = time.monotonic()
        try:
            run_with_hard_timeout(
                _abnormal_exit_worker,
                (str(abnormal_pid_path),),
                timeout_seconds,
            )
        except ProviderError as error:
            if "子进程异常退出" not in str(error) or "退出码 23" not in str(error):
                raise WorkerDiagnosticsError(f"异常退出报告不完整: {error}") from error
            abnormal_pid = _read_pid(abnormal_pid_path, "异常退出")
            worker_pids.append(abnormal_pid)
            cases["abnormal_exit"] = {
                "pid": abnormal_pid,
                "exception_type": type(error).__name__,
                "exit_code": 23,
                "elapsed_seconds": _elapsed(started),
            }
        else:
            raise WorkerDiagnosticsError("异常退出 worker 未产生 ProviderError")

        timeout_pid_path = root / "hard-timeout.pid"
        started = time.monotonic()
        try:
            run_with_hard_timeout(
                _timeout_worker,
                (str(timeout_pid_path), timeout_seconds + 60.0),
                timeout_seconds,
            )
        except ProviderTimeout as error:
            timeout_pid = _read_pid(timeout_pid_path, "硬超时")
            worker_pids.append(timeout_pid)
            cases["hard_timeout"] = {
                "pid": timeout_pid,
                "exception_type": type(error).__name__,
                "timeout_seconds": timeout_seconds,
                "elapsed_seconds": _elapsed(started),
            }
        else:
            raise WorkerDiagnosticsError("阻塞 worker 未产生 ProviderTimeout")

    residual_pids = [pid for pid in worker_pids if _pid_is_alive(pid)]
    if residual_pids:
        raise WorkerDiagnosticsError(f"诊断结束后仍有 worker 存活: {residual_pids}")
    return {
        "ok": True,
        "start_method": "spawn",
        "cases": cases,
        "worker_pids": worker_pids,
        "residual_pids": residual_pids,
    }
