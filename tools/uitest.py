"""界面层自动测试（离屏运行，不会弹窗）。

验证界面确实只是命令总线的一个客户端：
- 打开照片后，画布、参数行、状态栏都要同步；
- agent 从别的线程发来的命令，界面必须自动跟着变；
- 拖动过程中的中间态不能污染历史记录。

    python tools/uitest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from openlight.api.gateway import CommandGateway  # noqa: E402
from openlight.api.server import ApiServer  # noqa: E402
from openlight.config import Settings  # noqa: E402
from openlight.core.invoker import MainThreadInvoker  # noqa: E402
from openlight.core.session import Session  # noqa: E402
from openlight.ui import theme  # noqa: E402
from openlight.ui.main_window import MainWindow  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    RESULTS.append((bool(condition), label))
    print(("  [ok]   " if condition else "  [FAIL] ") + label)


def pump(app: QApplication, invoker: MainThreadInvoker, seconds: float = 0.6) -> None:
    """跑一会儿事件循环，让渲染线程的结果送到界面上。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        invoker.drain()
        time.sleep(0.01)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="openlight-uitest-"))
    source = workdir / "scene.jpg"
    height, width = 900, 1400
    ramp = np.tile(np.linspace(20, 235, width, dtype=np.uint8), (height, 1))
    tint = np.zeros((height, width, 3), np.uint8)
    tint[..., 0] = ramp
    tint[..., 1] = np.clip(ramp.astype(int) - 15, 0, 255).astype(np.uint8)
    tint[..., 2] = np.clip(ramp.astype(int) + 20, 0, 255).astype(np.uint8)
    Image.fromarray(tint, "RGB").save(source, quality=95)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.stylesheet())

    settings = Settings()
    settings.api_port = 0
    settings.preview_max_side = 640
    session = Session(settings)
    invoker = MainThreadInvoker()
    gateway = CommandGateway(session, invoker)
    api = ApiServer(session, "127.0.0.1", 0, gateway=gateway)
    base_url = api.start()
    window = MainWindow(session, api, invoker)
    window.show()
    pump(app, invoker, 0.3)

    try:
        print("1) 初始状态")
        check(not window.canvas.has_image(), "没有照片时画布为空")
        check(window.empty_overlay.isVisible(), "显示空状态提示")
        check(not window.action_export.isEnabled(), "没照片时导出按钮禁用")
        check("Agent 服务" in window.api_indicator.text(), "状态栏显示 agent 服务地址")

        print("\n2) 打开照片（模拟界面操作）")
        session.execute({"cmd": "document.open", "params": {"path": str(source)},
                         "source": "gui"})
        pump(app, invoker, 1.2)
        check(window.canvas.has_image(), "画布拿到画面")
        check(window.canvas.image_size() == (1400, 900) or window.canvas.image_size() == (640, 411),
              f"画布尺寸合理：{window.canvas.image_size()}")
        check(window.file_label.text() == "scene.jpg", "右侧面板显示文件名")
        check("1400" in window.dimension_label.text(), "状态栏显示分辨率")
        check(bool(window.histogram._channels), "直方图有数据")
        check(window.action_export.isEnabled(), "导出按钮启用")
        check(not window.action_undo.isEnabled(), "刚打开时没有可撤销步骤")

        print("\n3) 拖动滑块：中间态不进历史")
        row = window._rows["exposure"]  # noqa: SLF001
        history_before = len(session.history.entries())
        row.preview.emit("exposure", 0.75)
        row.preview.emit("exposure", 1.5)
        pump(app, invoker, 0.8)
        check(len(session.history.entries()) == history_before,
              "拖动过程中没有写入历史记录")
        check(window._draft_params is not None, "界面持有的草稿参数生效")  # noqa: SLF001
        row.committed.emit("exposure", 1.5)
        pump(app, invoker, 1.0)
        check(len(session.history.entries()) == history_before + 1,
              "松手后只写入一步历史")
        check(session.document.params["exposure"] == 1.5, "命令总线里的参数已更新")
        check(window.action_undo.isEnabled(), "撤销按钮变为可用")

        print("\n4) agent 从别的线程发命令，界面要自动跟随")
        errors: list = []

        def agent_work() -> None:
            try:
                gateway.execute({"cmd": "adjustment.set",
                                 "params": {"id": "exposure", "value": -0.4}},
                                source="agent")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        thread = threading.Thread(target=agent_work, daemon=True)
        thread.start()
        pump(app, invoker, 1.6)
        thread.join(timeout=5)
        check(not errors, "跨线程命令没有报错")
        check(abs(row.value() - (-0.4)) < 1e-6,
              f"界面滑块自动跟着 agent 的命令变了（当前 {row.value()}）")
        check(abs(session.document.params["exposure"] - (-0.4)) < 1e-6, "内核参数同步")

        print("\n5) 对比 / 分屏")
        window.action_compare.setChecked(True)
        pump(app, invoker, 1.0)
        check(window.canvas._comparison is True, "对比模式开启")  # noqa: SLF001
        window.action_split.setChecked(True)
        pump(app, invoker, 1.0)
        check(window.canvas._split_mode is True and not window.action_compare.isChecked(),
              "分屏对比会和按住对比互斥")
        window.action_split.setChecked(False)
        pump(app, invoker, 0.4)

        print("\n6) 撤销 / 重置 回到默认")
        session.execute({"cmd": "adjustments.reset_all", "source": "gui"})
        pump(app, invoker, 1.0)
        check(abs(row.value()) < 1e-6, "重置后滑块回到 0")
        session.execute({"cmd": "history.undo"})
        pump(app, invoker, 1.0)
        check(abs(row.value() - (-0.4)) < 1e-6, "撤销后滑块回到上一步的值")

        print("\n7) 分组面板")
        groups = list(window._sections)
        check(groups == ["白平衡", "基本", "偏好", "色调曲线", "曲线", "HSL 混色器"],
              f"侧栏按注册表分成 6 组：{'、'.join(groups)}")
        check(window._rows["exposure"].isVisible() and window._sections["基本"].is_expanded(),
              "常用的「基本」组默认展开")
        check(not window.curve_editor.isVisible(),
              "「曲线」组默认收起（面板不至于一屏塞不下）")
        check(len(window._rows) == 38, f"面板上共 38 个滑块行（实际 {len(window._rows)}）")
        check(window._sections["曲线"].body.sizeHint().height() > 200,
              "曲线组里塞着编辑器，不是空的")

        print("\n8) 曲线编辑器")
        window._sections["曲线"].set_expanded(True)  # noqa: SLF001
        pump(app, invoker, 0.4)
        check(window.curve_editor.isVisible(), "展开后曲线编辑器可见")
        # 用「曲线标签的步数」来数：上一节刚撤销过，历史里还有 redo 尾巴，
        # 直接比总长度会把「新分支砍掉 redo」算成没变化。
        def curve_steps() -> int:
            return len([e for e in session.history.entries() if "曲线" in e.label])

        steps_before_curve = curve_steps()
        editor = window.curve_editor
        target = editor._to_widget(0.5, 0.72)  # noqa: SLF001
        width, height = editor.width(), editor.height()
        QTest.mousePress(editor, Qt.LeftButton, Qt.NoModifier,
                         QPoint(int(target[0]), int(target[1])))
        QTest.mouseMove(editor, QPoint(int(target[0]), int(target[1]) - 4))
        QTest.mouseRelease(editor, Qt.LeftButton, Qt.NoModifier,
                           QPoint(int(target[0]), int(target[1]) - 4))
        pump(app, invoker, 1.0)
        points = session.document.params["curve_rgb"]
        check(len(points) == 3, f"在曲线上单击加点（现在 {len(points)} 个控制点）")
        check(abs(points[1][1] - 0.72) < 0.08,
              f"新点拖到了大致位置（y={points[1][1]}）")
        check(curve_steps() == steps_before_curve + 1,
              f"整段拖动只写一步历史（曲线步数 {steps_before_curve} -> {curve_steps()}）")
        check(editor.adjustment_id() == "curve_rgb" and editor.height() > 100,
              f"曲线编辑器尺寸合理（{width}x{height}）")

        window.curve_editor.set_points([[0, 0], [0.25, 0.18], [0.75, 0.85], [1, 1]],
                                       notify=True)
        pump(app, invoker, 1.0)
        check(session.document.params["curve_rgb"] ==
              [[0.0, 0.0], [0.25, 0.18], [0.75, 0.85], [1.0, 1.0]],
              "曲线命令把控制点送进了命令总线")
        window.curve_combo.setCurrentIndex(3)      # 蓝通道
        pump(app, invoker, 0.4)
        check(window.curve_editor.adjustment_id() == "curve_blue"
              and window.curve_editor.points() == [[0.0, 0.0], [1.0, 1.0]],
              "切换通道会显示对应的那条曲线")
        session.execute({"cmd": "adjustment.set",
                         "params": {"id": "curve_blue", "value": [[0, 0.1], [1, 0.9]]}})
        pump(app, invoker, 0.8)
        check(window.curve_editor.points() == [[0.0, 0.1], [1.0, 0.9]],
              "agent 改的曲线会同步到编辑器上")
        window._on_group_reset("曲线")  # noqa: SLF001
        pump(app, invoker, 0.8)
        check(session.document.params["curve_rgb"] == [[0.0, 0.0], [1.0, 1.0]]
              and session.document.params["curve_blue"] == [[0.0, 0.0], [1.0, 1.0]],
              "「复位本组」把两条曲线一次复位")

        print("\n9) HSL 混色器")
        window._sections["HSL 混色器"].set_expanded(True)  # noqa: SLF001
        window.hsl_mode.setCurrentIndex(2)                # 明亮度
        pump(app, invoker, 0.4)
        check(window._rows["hsl_blue_lum"].isVisible()
              and not window._rows["hsl_blue_sat"].isVisible(),
              "切到「明亮度」后只显示明亮度滑块")
        check(len([key for key, row in window._rows.items()
                   if key.startswith("hsl_") and row.isVisible()]) == 8,
              "一次显示 8 个色带")
        window._rows["hsl_blue_lum"].preview.emit("hsl_blue_lum", 20)
        window._rows["hsl_blue_lum"].preview.emit("hsl_blue_lum", 30)
        pump(app, invoker, 0.6)
        hsl_steps = len([e for e in session.history.entries() if "明亮度" in e.label])
        check(session.document.params["hsl_blue_lum"] == 0,
              "拖动中的 HSL 中间态同样不写历史")
        window._rows["hsl_blue_lum"].set_value(35, notify=True)
        pump(app, invoker, 1.0)
        check(session.document.params["hsl_blue_lum"] == 35,
              "HSL 滑块的值进了命令总线")
        check(len([e for e in session.history.entries() if "明亮度" in e.label]) == hsl_steps + 1,
              "HSL 松手同样只写一步历史")
        check(window._sections["HSL 混色器"].badge.text().startswith("已调"),
              f"分组标题显示「{window._sections['HSL 混色器'].badge.text()}」")
        window._on_group_reset("HSL 混色器")               # noqa: SLF001
        pump(app, invoker, 0.8)
        check(session.document.params["hsl_blue_lum"] == 0,
              "「复位本组」能一次清掉整组 HSL 调整")
        session.execute({"cmd": "adjustments.reset_all", "source": "gui"})
        pump(app, invoker, 0.6)

        print("\n10) 事件控制台")
        text = window.console.toPlainText()
        check("document.opened" in text and "adjustment.changed" in text,
              "控制台记录了操作事件")
        check("[agent]" in text, "控制台区分了 agent 与人的操作来源")

        print("\n11) agent 通过 HTTP 影响界面")
        import json
        import urllib.request
        request = urllib.request.Request(
            f"{base_url}/command",
            data=json.dumps({"cmd": "adjustment.set",
                             "params": {"id": "exposure", "value": 2.0}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response_holder: list = []

        def http_work() -> None:
            with urllib.request.urlopen(request, timeout=20) as response:
                response_holder.append(json.loads(response.read().decode()))

        thread2 = threading.Thread(target=http_work, daemon=True)
        thread2.start()
        pump(app, invoker, 2.0)
        thread2.join(timeout=6)
        check(response_holder and response_holder[0]["ok"], "HTTP 命令执行成功")
        check(abs(row.value() - 2.0) < 1e-6,
              f"HTTP 过来的命令同样驱动了界面（当前 {row.value()}）")

        print("\n12) 关闭清理")
        window.close()
        pump(app, invoker, 0.3)
        check(True, "关闭窗口没有异常")
    finally:
        api.stop()

    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"失败 {len(failed)} 项：")
        for label in failed:
            print("  - " + label)
        return 1
    print(f"全部通过（{len(RESULTS)} 项）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
