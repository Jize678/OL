"""给界面出图：让程序自己渲染主窗口并存成 PNG（不依赖桌面抓屏）。

    python tools/screenshot.py out.png [照片路径]

不传照片路径时会自动生成一张测试图。开发时用它检查界面排版。
"""

from __future__ import annotations

import os
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
from openlight.ui import theme  # noqa: E402
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
    out = Path(args[0]) if args else Path("screenshot.png")
    photo = Path(args[1]) if len(args) > 1 else Path(tempfile.gettempdir()) / "openlight_demo.jpg"
    if not photo.exists():
        demo_photo(photo)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.stylesheet())

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
    pixmap = window.grab()
    pixmap.save(str(out))
    print(f"已保存界面截图：{out}（{pixmap.width()}x{pixmap.height()}）")
    api.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
