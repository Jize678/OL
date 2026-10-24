"""事件日志：所有状态变更都要留下痕迹。

agent 不应该是「盲操作后猜结果」。它可以：
- 执行命令，直接拿到返回的状态；
- 或者订阅事件流（HTTP: GET /events?since=N），拿到人手动操作的记录。

序号 seq 单调递增，方便 agent 做「我上次看到第 7 条，现在把 8 以后的给我」。
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Event:
    seq: int
    ts: float
    type: str
    source: str          # "gui" / "agent" / "system"
    data: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "source": self.source,
            "data": self.data,
        }


class EventLog:
    """线程安全的事件环形缓冲 + 订阅者通知。"""

    def __init__(self, capacity: int = 1000) -> None:
        self._buffer: deque[Event] = deque(maxlen=capacity)
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._subscribers: list = []
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def emit(self, type_: str, data: dict | None = None, source: str = "system") -> Event:
        with self._lock:
            event = Event(
                seq=next(self._counter),
                ts=time.time(),
                type=type_,
                source=source,
                data=data or {},
            )
            self._buffer.append(event)
            self._last_seq = event.seq
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # 订阅者出错不能影响主流程
                pass
        return event

    def list_since(self, since: int = 0, limit: int = 200) -> list[Event]:
        with self._lock:
            items = [e for e in self._buffer if e.seq > since]
        return items[-limit:]

    def subscribe(self, callback) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
