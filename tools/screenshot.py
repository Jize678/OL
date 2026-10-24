"""给界面出图：让程序自己渲染主窗口并存成 PNG（不依赖桌面抓屏）。

    python tools/screenshot.py out.png [照片路径]

不传照片路径时优先用仓库自带的 samples/demo.jpg；它不存在才现画一张测试图。
开发时用它检查界面排版。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if "--onscreen" not in sys.argv:
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
from PySide6.QtWidgets import QApplication  # noqa: E402

from openlight.api.gateway import CommandGateway  # noqa: E402
from openlight.api.server import ApiServer  # noqa: E402
from openlight.config import Settings  # noqa: E402
from openlight.core.invoker import MainThreadInvoker  # noqa: E402
from openlight.core.session import Session  # noqa: E402
from openlight.ui import fonts  # noqa: E402
from openlight.ui.app import configure_app  # noqa: E402
from openlight.ui.main_window import MainWindow  # noqa: E402


def demo_photo(path: Path) -> None:
    """合成的样张：天空渐变 + 太阳 + 地面，用来演示曝光。"""
    height, width = 1200, 1800
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    horizon = int(height * 0.62)
    sky = np.clip(1.0 - yy / height, 0, 1)
    image = np.zeros((height, width, 3), np.float32)
    for channel, top, bottom in ((0, 60, 210), (1, 105, 225), (2, 170, 240)):
        image[..., channel] = np.where(yy < horizon, top + (bottom - top) * sky,
                                       40 + 30 * (yy / height))
    ground = np.clip(0.9 - (yy - horizon) / (height - horizon) * 0.5, 0, 1)
    for channel, base in ((0, 90), (1, 105), (2, 70)):
        image[..., channel] = np.where(yy >= horizon, base * ground + 20, image[..., channel])
    rng = np.random.default_rng(7)
    image += rng.normal(0, 6, image.shape)
    sun = np.exp(-(((xx - width * 0.72) ** 2 + (yy - height * 0.24) ** 2)
                   / (2 * (width * 0.05) ** 2)))
    image[..., 0] += sun * 120
    image[..., 1] += sun * 110
    image[..., 2] += sun * 80
    Image.fromarray(np.clip(image, 0, 255).astype(np.uint8), "RGB").save(path, quality=94)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(args[0]) if args else Path("docs/screenshot-ui.png")
    # 沙箱里系统临时目录可能不可写，tempfile 会退回到当前目录；用独立子目录，用完就删，
    # 免得仓库根目录里多出一张 openlight_demo.jpg。
    workdir = Path(tempfile.mkdtemp(prefix="openlight-screenshot-"))
    try:
        return render(out, args, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def render(out: Path, args: list[str], workdir: Path) -> int:
    photo = Path(args[1]) if len(args) > 1 else ROOT / "samples" / "demo.jpg"
    if not photo.exists():
        photo = workdir / "demo.jpg"
        demo_photo(photo)

    app = QApplication.instance() or QApplication(sys.argv)
    family = configure_app(app)

    settings = Settings()
    settings.api_port = 0
    session = Session(settings)
    invoker = MainThreadInvoker()
    api = ApiServer(session, "127.0.0.1", 0, gateway=CommandGateway(session, invoker))
    api.start()

    window = MainWindow(session, api, invoker)
    window.resize(1440, 900)
    window.show()

    # 打开照片 + 让 agent 走一遍接口，好让控制台里有内容
    session.execute({"cmd": "document.open", "params": {"path": str(photo)},
                     "source": "gui"})
    time.sleep(0.4)
    for command in (
        {"cmd": "adjustments.set_batch",
         "params": {"adjustments": {"exposure": -0.35, "contrast": 18,
                                    "highlights": -30, "shadows": 25,
                                    "vibrance": 20}}},
        {"cmd": "curve.set_points",
         "params": {"id": "curve_rgb", "points": [[0, 0], [0.25, 0.2], [0.75, 0.84], [1, 1]]}},
        {"cmd": "preview.get", "params": {"max_side": 400}},
    ):
        session.execute({**command, "source": "agent"})
        time.sleep(0.4)

    # 展开曲线和 HSL 分组，让截图能体现界面的全部能力
    window._sections["曲线"].set_expanded(True)          # noqa: SLF001
    window._sections["HSL 混色器"].set_expanded(True)     # noqa: SLF001
    window.hsl_mode.setCurrentIndex(0)                    # 色相
    time.sleep(0.3)

    end = time.time() + 3.0
    while time.time() < end:
        app.processEvents()
        invoker.drain()
        time.sleep(0.01)

    out.parent.mkdir(parents=True, exist_ok=True)

    # 出图前先审一遍字体：宁可不出图，也不出一张满屏方框的图。
    problems = fonts.audit_widgets(window)
    if problems:
        print("截图已取消：界面里有字符缺字形（渲染出来会是豆腐块）：")
        for line in problems[:20]:
            print("  - " + line)
        print("可以设置环境变量 OPENLIGHT_FONT 指向一个中文字体文件后重试。")
        window.close()          # closeEvent 里会 renderer.shutdown()，否则进程退出时崩
        api.stop()
        return 2

    pixmap = window.grab()
    pixmap.save(str(out))
    print(f"已保存界面截图：{out}（{pixmap.width()}x{pixmap.height()}，字体 {family or '默认'}）")
    window.close()
    api.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
