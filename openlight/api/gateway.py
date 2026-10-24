"""命令入口封装：把「谁在调用」这件事和线程模型隔离开。"""

from __future__ import annotations

from ..core.invoker import DirectInvoker
from ..core.session import Session


class CommandGateway:
    def __init__(self, session: Session, invoker=None) -> None:
        self.session = session
        self.invoker = invoker or DirectInvoker()

    def execute(self, request: dict, source: str = "agent") -> dict:
        if isinstance(request, dict) and not request.get("source"):
            request = {**request, "source": source}
        try:
            return self.invoker.call(lambda: self.session.execute(request))
        except TimeoutError as exc:
            return {
                "ok": False,
                "cmd": (request or {}).get("cmd") if isinstance(request, dict) else None,
                "result": None,
                "state": None,
                "error": {"code": "timeout", "message": str(exc),
                          "hint": "确认界面没有卡住；无界面模式请用 openlight serve。"},
            }
