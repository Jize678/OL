"""历史栈：撤销 / 重做。

人和 agent 共用同一个历史栈——agent 调错参数后，用户可以按 Ctrl+Z 撤掉，
反过来用户在界面上的操作，agent 也能通过 history.undo 撤回。
参数快照很小，所以直接存快照不做 diff；但曲线是嵌套列表，必须深拷贝。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HistoryEntry:
    label: str                       # 人类可读，如「曝光 +0.80 EV」
    source: str                      # "gui" / "agent" / "system"，便于事后审计
    params: dict[str, Any]           # 这一步之后的完整参数快照

    def to_dict(self) -> dict:
        return {"label": self.label, "source": self.source,
                "params": deepcopy(self.params)}


@dataclass
class History:
    limit: int = 300
    _entries: list[HistoryEntry] = field(default_factory=list)
    _cursor: int = -1                # 当前状态在 _entries 中的位置

    # -- 查询 ---------------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._entries) - 1

    @property
    def current(self) -> HistoryEntry | None:
        if 0 <= self._cursor < len(self._entries):
            return self._entries[self._cursor]
        return None

    def entries(self) -> list[HistoryEntry]:
        return list(self._entries)

    def to_dict(self) -> dict:
        return {
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
            "cursor": self._cursor,
            "count": len(self._entries),
            "entries": [entry.to_dict() for entry in self._entries],
        }

    # -- 写入 ---------------------------------------------------------------
    def reset(self, params: dict, label: str = "打开照片") -> None:
        self._entries = [HistoryEntry(label=label, source="system",
                                      params=deepcopy(params))]
        self._cursor = 0

    def push(self, params: dict, label: str, source: str = "gui") -> None:
        if self.current is not None and self.current.params == params:
            return  # 参数没变，不进历史，避免脏记录
        del self._entries[self._cursor + 1:]      # 新分支砍掉 redo 链
        self._entries.append(HistoryEntry(label=label, source=source,
                                          params=deepcopy(params)))
        if len(self._entries) > self.limit:
            self._entries.pop(0)
        self._cursor = len(self._entries) - 1

    def undo(self) -> HistoryEntry | None:
        if not self.can_undo:
            return None
        self._cursor -= 1
        return self.current

    def redo(self) -> HistoryEntry | None:
        if not self.can_redo:
            return None
        self._cursor += 1
        return self.current