"""曲线编辑器：可以拖点的色调曲线控件。

和滑块一样遵守同一条规矩 —— 拖动过程中只发 preview（不写历史），松手才发
committed，由主窗口翻译成 `curve.set_points` 命令。控件本身不碰 session。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..core.curves import identity_points, monotone_lut, normalize_points
from .theme import COLORS

# 每条曲线的颜色：RGB 用白色，单通道用本色，和修图软件的直觉一致
CURVE_COLORS = {
    "rgb": "#e6e8ec",
    "red": "#e06c75",
    "green": "#5cd682",
    "blue": "#5c96ff",
}

_HIT_RADIUS = 11        # 命中控制点的像素半径
_MIN_GAP = 0.02         # 控制点在 x 轴上的最小间距
_PADDING = 10


class CurveEditor(QWidget):
    """点曲线编辑器。

    操作：单击空白处加一个点并立刻拖动；拖动已有的点；右键或双击删掉一个点。
    端点只允许上下移动（x 固定在 0 和 1）。
    """

    preview = Signal(str, object)      # adjustment_id, points
    committed = Signal(str, object)    # adjustment_id, points

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._adjustment_id = "curve_rgb"
        self._channel = "rgb"
        self._points: list[list[float]] = identity_points()
        self._drag_index: int | None = None
        self._hover_index: int | None = None
        self._histogram: np.ndarray | None = None
        self.setMinimumHeight(196)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_adjustment(self, adjustment_id: str, channel: str,
                       points=None) -> None:
        self._adjustment_id = adjustment_id
        self._channel = channel
        self._points = normalize_points(points)
        self.update()

    def adjustment_id(self) -> str:
        return self._adjustment_id

    def points(self) -> list[list[float]]:
        return [list(point) for point in self._points]

    def set_points(self, points, *, notify: bool = False) -> None:
        self._points = normalize_points(points)
        self.update()
        if notify:
            self.committed.emit(self._adjustment_id, self.points())

    def reset(self, *, notify: bool = True) -> None:
        self._points = identity_points()
        self.update()
        if notify:
            self.committed.emit(self._adjustment_id, self.points())

    def set_histogram(self, channels: dict | None) -> None:
        """把当前画面的直方图垫在曲线后面，方便判断该往哪儿拉。"""
        if not channels:
            self._histogram = None
            self.update()
            return
        red = np.asarray(channels.get("r") or [], dtype=np.float32)
        green = np.asarray(channels.get("g") or [], dtype=np.float32)
        blue = np.asarray(channels.get("b") or [], dtype=np.float32)
        if not (len(red) == len(green) == len(blue) == 256):
            self._histogram = None
        else:
            self._histogram = (red + green + blue) / 3.0
        self.update()

    # ------------------------------------------------------------------
    # 坐标换算
    # ------------------------------------------------------------------
    def _plot_rect(self):
        return self.rect().adjusted(_PADDING, _PADDING, -_PADDING, -_PADDING)

    def _to_widget(self, x: float, y: float) -> tuple[float, float]:
        rect = self._plot_rect()
        return (rect.left() + x * rect.width(),
                rect.bottom() - y * rect.height())

    def _to_value(self, px: float, py: float) -> tuple[float, float]:
        rect = self._plot_rect()
        x = (px - rect.left()) / max(1.0, rect.width())
        y = (rect.bottom() - py) / max(1.0, rect.height())
        return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)

    def _hit(self, px: float, py: float) -> int | None:
        best: int | None = None
        best_distance = _HIT_RADIUS
        for index, (x, y) in enumerate(self._points):
            wx, wy = self._to_widget(x, y)
            distance = ((wx - px) ** 2 + (wy - py) ** 2) ** 0.5
            if distance <= best_distance:
                best = index
                best_distance = distance
        return best

    # ------------------------------------------------------------------
    # 鼠标
    # ------------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802
        position = event.position()
        index = self._hit(position.x(), position.y())
        if event.button() == Qt.RightButton:
            if index is not None and 0 < index < len(self._points) - 1:
                del self._points[index]
                self.update()
                self.committed.emit(self._adjustment_id, self.points())
            return

        if index is None:
            x, y = self._to_value(position.x(), position.y())
            self._points.append([x, y])
            self._points.sort(key=lambda point: point[0])
            index = next(i for i, point in enumerate(self._points) if point[0] == x)
        self._drag_index = index
        self._hover_index = index
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        position = event.position()
        if self._drag_index is None:
            hover = self._hit(position.x(), position.y())
            if hover != self._hover_index:
                self._hover_index = hover
                self.update()
            return

        x, y = self._to_value(position.x(), position.y())
        index = self._drag_index
        count = len(self._points)
        if index == 0:
            x = 0.0
            upper = self._points[1][0] - _MIN_GAP if count > 1 else 1.0
            x = min(x, upper)
        elif index == count - 1:
            x = 1.0
            lower = self._points[index - 1][0] + _MIN_GAP
            x = max(x, lower)
        else:
            lower = self._points[index - 1][0] + _MIN_GAP
            upper = self._points[index + 1][0] - _MIN_GAP
            x = min(max(x, lower), upper)

        self._points[index] = [round(x, 4), round(y, 4)]
        self.update()
        self.preview.emit(self._adjustment_id, self.points())

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag_index is None:
            return
        self._drag_index = None
        self.committed.emit(self._adjustment_id, self.points())

    def leaveEvent(self, event):  # noqa: N802
        self._hover_index = None
        self.update()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        position = event.position()
        index = self._hit(position.x(), position.y())
        if index is not None and 0 < index < len(self._points) - 1:
            del self._points[index]
            self._hover_index = None
            self.update()
            self.committed.emit(self._adjustment_id, self.points())

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self._plot_rect()
        painter.fillRect(self.rect(), QColor(COLORS["canvas"]))

        self._paint_histogram(painter, rect)
        self._paint_grid(painter, rect)
        self._paint_curve(painter)
        self._paint_points(painter)
        painter.end()

    def _paint_histogram(self, painter: QPainter, rect) -> None:
        if self._histogram is None:
            return
        values = np.log1p(self._histogram)
        peak = float(values.max()) or 1.0
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom())
        for index, value in enumerate(values):
            x = rect.left() + rect.width() * index / (len(values) - 1)
            y = rect.bottom() - rect.height() * (value / peak) * 0.92
            path.lineTo(x, y)
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        painter.fillPath(path, QColor(255, 255, 255, 18))

    def _paint_grid(self, painter: QPainter, rect) -> None:
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        for i in range(1, 4):
            x = rect.left() + rect.width() * i / 4
            y = rect.top() + rect.height() * i / 4
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        pen = QPen(QColor(COLORS["text_dim"]), 1, Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.top())
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.drawRect(rect)

    def _paint_curve(self, painter: QPainter) -> None:
        color = QColor(CURVE_COLORS.get(self._channel, COLORS["text"]))
        lut = monotone_lut(self._points, size=256)
        path = QPainterPath()
        for index in range(len(lut)):
            x, y = self._to_widget(index / (len(lut) - 1), float(lut[index]))
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(color, 2))
        painter.drawPath(path)

    def _paint_points(self, painter: QPainter) -> None:
        color = QColor(CURVE_COLORS.get(self._channel, COLORS["text"]))
        for index, (x, y) in enumerate(self._points):
            wx, wy = self._to_widget(x, y)
            radius = 5 if index in (self._drag_index, self._hover_index) else 3.5
            painter.setPen(QPen(QColor(COLORS["canvas"]), 1))
            painter.setBrush(color)
            painter.drawEllipse(int(wx - radius), int(wy - radius),
                                int(radius * 2), int(radius * 2))

        if self._drag_index is not None:
            x, y = self._points[self._drag_index]
            painter.setPen(QColor(COLORS["text"]))
            painter.drawText(self.rect().adjusted(6, 4, -6, 0), Qt.AlignTop | Qt.AlignLeft,
                             f"{x:.3f} → {y:.3f}")