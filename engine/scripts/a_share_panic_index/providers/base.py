"""Provider Contract、请求重试和硬超时。"""

from __future__ import annotations

import multiprocessing
import random
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests


class ProviderError(RuntimeError):
    pass


class ProviderRetryableError(ProviderError):
    """连接、超时或可恢复HTTP错误。"""


class ProviderUnavailable(ProviderError):
    pass


class ProviderTimeout(ProviderRetryableError):
    pass


class ProviderDataError(ProviderError):
    pass


class ProviderTransportError(ProviderRetryableError):
    pass


class HttpClient:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                )
            }
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        timeout = float(kwargs.pop("timeout", self.timeout))
        try:
            response = self.session.get(url, timeout=timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as error:
            raise ProviderTransportError(str(error)) from error
        if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
            raise ProviderRetryableError(f"可恢复HTTP错误 {response.status_code}")
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise ProviderDataError(f"不可恢复HTTP错误 {response.status_code}") from error
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type and "<html" in response.text[:200].lower():
            raise ProviderDataError("数据接口返回HTML错误页")
        return response


def retry_call(
    operation: Callable[[], Any],
    max_retries: int,
    delay_seconds: float,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(1, int(max_retries))):
        try:
            return operation()
        except ProviderRetryableError as error:
            last_error = error
            if attempt + 1 >= max_retries:
                break
            delay = delay_seconds * (2**attempt) + random.uniform(0, delay_seconds)
            time.sleep(delay)
    if last_error:
        raise last_error
    raise ProviderError("数据源请求失败")


def run_with_hard_timeout(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    timeout_seconds: float,
) -> Any:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_process_entry,
        args=(child_connection, target, args),
    )
    process.start()
    child_connection.close()
    if not parent_connection.poll(timeout_seconds):
        process.terminate()
        process.join(5)
        parent_connection.close()
        raise ProviderTimeout(f"数据源硬超时 {timeout_seconds:g} 秒")
    try:
        ok, payload = parent_connection.recv()
    except EOFError as error:
        raise ProviderError(f"数据源子进程异常退出，退出码 {process.exitcode}") from error
    finally:
        parent_connection.close()
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if ok:
        return payload
    error_type, message = payload
    exception = {
        "ProviderUnavailable": ProviderUnavailable,
        "ProviderDataError": ProviderDataError,
        "ProviderTimeout": ProviderTimeout,
        "ProviderRetryableError": ProviderRetryableError,
        "ProviderTransportError": ProviderTransportError,
    }.get(error_type, ProviderError)
    raise exception(message)


def _process_entry(connection, target: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        connection.send((True, target(*args)))
    except Exception as error:
        connection.send((False, (type(error).__name__, str(error))))
    finally:
        connection.close()


def shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def parse_source_time(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        text = str(value).strip().replace("/", "-")
        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        )
        parsed = None
        for pattern in formats:
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as error:
                raise ProviderDataError(f"无法解析来源时间: {value}") from error
    else:
        parsed = fallback or shanghai_now()
    timezone = ZoneInfo("Asia/Shanghai")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)
