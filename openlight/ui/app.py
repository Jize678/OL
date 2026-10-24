"""应用入口：把内核、命令总线、HTTP 服务、界面串起来。"""

from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .. import APP_NAME, __version__
from ..api.gateway import CommandGateway
from ..api.server import ApiServer
from ..config import Settings
from ..core.invoker import MainThreadInvoker
from ..core.session import Session
from . import theme
from .main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.stylesheet())

    settings = Settings.load()
    session = Session(settings)

    # 界面线程与 HTTP 线程之间的桥：HTTP 请求排进队列，由下面的定时器在主线程执行，
    # 这样 Qt 对象永远只在主线程被碰。
    invoker = MainThreadInvoker()
    gateway = CommandGateway(session, invoker)

    api = None
    api_error = None
    if settings.api_enabled:
        api = ApiServer(session, settings.api_host, settings.api_port, gateway=gateway)
        try:
            api.start()
        except Exception as exc:
            api_error = str(exc)
            api = None

    window = MainWindow(session, api, invoker)
    if api_error:
        window._set_status(f"Agent 服务启动失败：{api_error}", error=True)

    drain_timer = QTimer()
    drain_timer.setInterval(12)     # 每 12ms 处理一轮外部命令
    drain_timer.timeout.connect(invoker.drain)
    drain_timer.start()

    window.show()

    # 命令行里直接跟一个文件路径时，启动即打开它
    for argument in argv[1:]:
        if not argument.startswith("-"):
            QTimer.singleShot(150, lambda path=argument: window.open_path(path))
            break

    exit_code = app.exec()
    if api is not None:
        api.stop()
    return exit_code
