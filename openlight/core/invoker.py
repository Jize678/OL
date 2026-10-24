"""线程编排：让外部线程（HTTP 请求）能安全地操作界面线程里的状态。

Qt 的对象只能在创建它的线程里碰。所以 HTTP 线程**不直接**执行命令，而是
把任务塞进队列，由界面线程的定时器取出执行，再把结果回传给 HTTP 线程。

无界面模式（openlight serve）下，直接用 DirectInvoker 同步执行。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable


class DirectInvoker:
    """在调用者线程里直接执行。"""

    name = "direct"

    def call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        return fn()

    def drain(self, max_items: int = 32) -> int:
        return 0


@dataclass
class _Job:
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class MainThreadInvoker:
    """把任务排到界面线程执行。界面线程需要周期性调用 drain()。"""

    name = "main-thread"

    def __init__(self) -> None:
        self._queue: "queue.Queue[_Job]" = queue.Queue()

    def call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        job = _Job(fn=fn)
        self._queue.put(job)
        if not job.done.wait(timeout):
            raise TimeoutError("命令等待界面线程超时（界面可能没在响应）。")
        if job.error is not None:
            raise job.error
        return job.result

    def drain(self, max_items: int = 32) -> int:
        handled = 0
        while handled < max_items:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                job.result = job.fn()
            except BaseException as exc:  # 原样回传给调用方
                job.error = exc
            finally:
                job.done.set()
            handled += 1
        return handled
