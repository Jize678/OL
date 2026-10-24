"""后台渲染线程。

界面线程只负责贴图，像素运算全部丢给这里。请求会自动合并——拖动滑块时
只保留最新的那一次，中间那些过期请求直接丢掉，所以拖起来是跟手的。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np
from PIL import Image
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from ..core import engine


def pil_to_qimage(image: Image.Image) -> QImage:
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    array = np.asarray(rgb, dtype=np.uint8)
    height, width = array.shape[:2]
    # QImage 不持有 numpy 内存，必须 copy() 一份自己的
    return QImage(array.tobytes(), width, height, width * 3, QImage.Format_RGB888).copy()


def histogram_of(image: Image.Image) -> dict:
    array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if array.ndim != 3 or array.size == 0:
        return {}
    step = max(1, array.shape[0] // 512)
    sample = array[::step, ::step]
    return {
        "r": np.bincount(sample[..., 0].ravel(), minlength=256).tolist(),
        "g": np.bincount(sample[..., 1].ravel(), minlength=256).tolist(),
        "b": np.bincount(sample[..., 2].ravel(), minlength=256).tolist(),
    }


@dataclass
class RenderPayload:
    kind: str                     # "current" | "original"
    token: int
    image: QImage
    histogram: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    max_side: int = 0
    duration_ms: float = 0.0


class PreviewRenderer(QThread):
    rendered = Signal(object)     # RenderPayload

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._condition = threading.Condition()
        self._jobs: dict[str, tuple[int, dict, int]] = {}
        self._token = 0
        self._stop = False
        self._document = None

    # -- 供界面线程调用 -----------------------------------------------------
    def set_document(self, document) -> None:
        with self._condition:
            self._document = document
            self._jobs.clear()
            self._condition.notify()

    def request(self, kind: str, params: dict, max_side: int) -> int:
        with self._condition:
            self._token += 1
            token = self._token
            self._jobs[kind] = (token, dict(params), int(max_side))
            self._condition.notify()
            return token

    def shutdown(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify()
        self.wait(3000)

    # -- 线程主体 -----------------------------------------------------------
    def run(self) -> None:  # noqa: D102
        while True:
            with self._condition:
                while not self._stop and not self._jobs:
                    self._condition.wait(0.3)
                if self._stop:
                    return
                jobs = self._jobs
                self._jobs = {}
                document = self._document
            if document is None:
                continue

            for kind, (token, params, max_side) in jobs.items():
                try:
                    result = engine.engine.render(document.preview_source, params,
                                                  max_side=max_side)
                except Exception:
                    continue
                payload = RenderPayload(
                    kind=kind,
                    token=token,
                    image=pil_to_qimage(result.image),
                    histogram=histogram_of(result.image),
                    stats=result.stats.to_dict(),
                    params=params,
                    max_side=max_side,
                )
                self.rendered.emit(payload)
