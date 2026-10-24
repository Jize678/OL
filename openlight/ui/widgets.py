"""界面用的小部件。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import fonts
from .theme import COLORS

SLIDER_SCALE = 100  # 滑块用整数，除以 100 得到 0.01 精度的浮点值


class ClickableLabel(QLabel):
    """双击复位的标签。"""

    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class AdjustmentRow(QWidget):
    """一行调整项：名称 + 滑块 + 数值框 + 复位。

    拖动过程中只发 preview 信号（不写历史），松手才发 committed 信号。
    这样用户拖一次滑块只产生一步历史，而不是几百步。
    """

    preview = Signal(str, float)     # id, value —— 拖拽中，仅刷新预览
    committed = Signal(str, float)   # id, value —— 松手，写入命令总线
    reset_requested = Signal(str)

    def __init__(self, spec, parent=None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self.spec = spec
        self.compact = compact

        self.name_label = ClickableLabel(spec.label)
        self.name_label.setFixedWidth(46 if compact else 58)
        self.name_label.setCursor(Qt.PointingHandCursor)
        self.name_label.setToolTip(f"{spec.description}\n（双击复位）")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(spec.minimum * SLIDER_SCALE), int(spec.maximum * SLIDER_SCALE))
        self.slider.setSingleStep(int(spec.step * SLIDER_SCALE))
        self.slider.setPageStep(int(0.1 * SLIDER_SCALE))
        self.slider.setToolTip(f"{spec.description}\n单位：{spec.unit}，范围 "
                               f"{spec.minimum:g} ~ {spec.maximum:g}")

        self.spin = QDoubleSpinBox()
        self.spin.setRange(spec.minimum, spec.maximum)
        self.spin.setSingleStep(spec.step)
        self.spin.setDecimals(spec.precision)
        self.spin.setFixedWidth(74)
        self.spin.setSuffix(f" {spec.unit}" if spec.unit else "")
        self.spin.setToolTip(spec.description)

        # 说明文字吃高度：42 个参数全铺开太长，紧凑模式（HSL 混色器）只留 tooltip
        self.unit_label = QLabel(spec.agent_hint)
        self.unit_label.setStyleSheet(f"color:{COLORS['text_dim']};")
        self.unit_label.setWordWrap(True)
        self.unit_label.setVisible(not compact)

        self.spin.setFixedWidth(60 if compact else 74)

        header = QHBoxLayout()
        header.setSpacing(6 if compact else 8)
        header.addWidget(self.name_label)
        header.addWidget(self.slider, 1)
        header.addWidget(self.spin)

        self.reset_button = QPushButton("复位")
        self.reset_button.setFixedWidth(46)
        self.reset_button.setToolTip("恢复默认值")
        self.reset_button.setVisible(not compact)
        header.addWidget(self.reset_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2 if compact else 4, 0, 2 if compact else 4)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self.unit_label)

        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderReleased.connect(self._commit)
        self.spin.valueChanged.connect(self._on_spin)
        self.spin.editingFinished.connect(self._commit)
        self.reset_button.clicked.connect(lambda: self.reset_requested.emit(spec.id))
        self.name_label.doubleClicked.connect(lambda: self.reset_requested.emit(spec.id))

        self._updating = False
        self.set_value(spec.default)

    # -- 同步 ---------------------------------------------------------------
    def set_value(self, value: float, *, notify: bool = False) -> None:
        self._updating = True
        self.slider.setValue(int(round(value * SLIDER_SCALE)))
        self.spin.setValue(value)
        self._updating = False
        if notify:
            self.committed.emit(self.spec.id, value)
        self._style_value(value)

    def value(self) -> float:
        return self.spin.value()

    def _style_value(self, value: float) -> None:
        active = abs(value - self.spec.default) > 1e-9
        color = COLORS["accent"] if active else COLORS["text"]
        self.name_label.setStyleSheet(f"color:{color};")

    # -- 内部事件 -----------------------------------------------------------
    def _on_slider(self, raw: int) -> None:
        value = raw / SLIDER_SCALE
        if self._updating:
            return
        self._updating = True
        self.spin.setValue(value)
        self._updating = False
        self._style_value(value)
        self.preview.emit(self.spec.id, value)

    def _on_spin(self, value: float) -> None:
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(int(round(value * SLIDER_SCALE)))
        self._updating = False
        self._style_value(value)
        self.preview.emit(self.spec.id, value)

    def contextMenuEvent(self, event):  # noqa: N802
        """右键复位：紧凑模式下没有复位按钮，靠它补上。"""
        self.reset_requested.emit(self.spec.id)
        event.accept()

    def _commit(self) -> None:
        if self._updating:
            return
        self.committed.emit(self.spec.id, self.spin.value())


class CollapsibleSection(QWidget):
    """可收起的调整分组。

    标题左边是展开箭头，右边显示「已调 N 项」——收起状态下也能一眼看出
    这组里有没有动过东西，这是参数一多以后最要紧的一件事。
    """

    toggled = Signal(bool)
    reset_requested = Signal(str)

    def __init__(self, group_id: str, title: str, *, expanded: bool = True,
                 resettable: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.group_id = group_id
        self._expanded = expanded

        self.header = QToolButton()
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setCursor(Qt.PointingHandCursor)
        # 分组标题是侧栏的骨架：必须一直有字（曾经忘了 setText，整条标题栏是空白）。
        self.header.setObjectName("sectionHeader")
        self.set_title(title)

        self.badge = QLabel("")
        self.badge.setStyleSheet(f"color:{COLORS['accent']};")

        self.reset_button = QPushButton("复位本组")
        self.reset_button.setFixedWidth(66)
        self.reset_button.setToolTip("把这一组里动过的参数一次恢复到默认值（只占一步历史）")
        self.reset_button.setVisible(resettable)
        self.reset_button.clicked.connect(lambda: self.reset_requested.emit(self.group_id))

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        top.addWidget(self.header, 1)
        top.addWidget(self.badge)
        top.addWidget(self.reset_button)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(2, 2, 2, 6)
        self.body_layout.setSpacing(2)
        self.body.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self.body)

        self.header.clicked.connect(self._on_header)

    def set_title(self, title: str) -> None:
        self.header.setText(title)

    def add_widget(self, widget: QWidget) -> None:
        self.body_layout.addWidget(widget)

    def add_layout(self, layout) -> None:
        self.body_layout.addLayout(layout)

    def set_active_count(self, count: int) -> None:
        self.badge.setText(f"已调 {count} 项" if count else "")

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.header.setChecked(self._expanded)
        self.header.setArrowType(Qt.DownArrow if self._expanded else Qt.RightArrow)
        self.body.setVisible(self._expanded)
        self.toggled.emit(self._expanded)

    def _on_header(self) -> None:
        self.set_expanded(self.header.isChecked())


class HistogramWidget(QWidget):
    """RGB 直方图 + 削波提示。横轴是亮度，纵轴是对数计数。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(96)
        self._channels: dict[str, list[int]] = {}
        self._clipped_highlights = 0.0
        self._clipped_shadows = 0.0
        self.setToolTip("直方图：越靠右越亮。左右两端堆高说明高光或暗部被削掉了。")

    def set_data(self, channels: dict, clipped_highlights: float, clipped_shadows: float) -> None:
        self._channels = channels or {}
        self._clipped_highlights = clipped_highlights
        self._clipped_shadows = clipped_shadows
        self.update()

    def clear(self) -> None:
        self._channels = {}
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["canvas"]))
        rect = self.rect().adjusted(1, 1, -1, -1)

        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        for i in range(1, 4):
            x = rect.left() + rect.width() * i / 4
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())

        if not self._channels:
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(rect, Qt.AlignCenter, "打开照片后显示直方图")
            painter.end()
            return

        import math

        colors = {"r": QColor(255, 92, 92, 150), "g": QColor(92, 230, 130, 150),
                  "b": QColor(96, 150, 255, 150)}
        peak = 1
        for values in self._channels.values():
            scaled = [math.log1p(v) for v in values]
            if scaled:
                peak = max(peak, max(scaled))

        for key in ("r", "g", "b"):
            values = self._channels.get(key)
            if not values:
                continue
            painter.setPen(QPen(colors[key], 1))
            points = []
            for index, value in enumerate(values):
                x = rect.left() + rect.width() * index / max(1, len(values) - 1)
                y = rect.bottom() - rect.height() * (math.log1p(value) / peak)
                points.append((x, y))
            for i in range(1, len(points)):
                painter.drawLine(int(points[i - 1][0]), int(points[i - 1][1]),
                                 int(points[i][0]), int(points[i][1]))

        if self._clipped_highlights > 0.5:
            painter.setPen(QColor(COLORS["warning"]))
            painter.drawText(rect.adjusted(6, 0, 0, 0), Qt.AlignTop | Qt.AlignLeft,
                             f"高光削波 {self._clipped_highlights:.1f}%")
        if self._clipped_shadows > 0.5:
            painter.setPen(QColor(COLORS["danger"]))
            painter.drawText(rect.adjusted(6, 14, 0, 0), Qt.AlignTop | Qt.AlignLeft,
                             f"暗部削波 {self._clipped_shadows:.1f}%")
        painter.end()


class AgentConsole(QPlainTextEdit):
    """事件流窗口：人能看到 agent 做了什么，agent 也能看到人做了什么。"""

    MAX_BLOCKS = 400

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(self.MAX_BLOCKS)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        # 等宽字体一般不带汉字，事件摘要里的中文会变成方框：
        # 把启动时解析出的中文字族接在后面当兜底（Qt 会按字逐个回退）。
        fallback = fonts.default_family()
        if fallback and fallback != "Cascadia Mono":
            font.setFamilies(["Cascadia Mono", fallback])
        self.setFont(font)
        self.setPlaceholderText("这里会实时显示命令总线上的所有操作（灰色＝人操作，绿色＝agent 操作）")

    def append_event(self, event) -> None:
        color = {"agent": COLORS["agent"], "gui": COLORS["text_dim"],
                 "system": COLORS["accent"]}.get(event.source, COLORS["text"])
        stamp = _format_time(event.ts)
        summary = _summarize(event)
        self.appendHtml(
            f'<span style="color:{COLORS["text_dim"]}">#{event.seq} {stamp}</span> '
            f'<span style="color:{color}">[{event.source}]</span> '
            f'<span style="color:{COLORS["text"]}">{event.type}</span> '
            f'<span style="color:{COLORS["text_dim"]}">{summary}</span>'
        )


def _format_time(ts: float) -> str:
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts))


def _summarize(event) -> str:
    import json

    data = event.data or {}
    if event.type == "adjustment.changed":
        if data.get("batch"):
            return json.dumps(data.get("adjustments"), ensure_ascii=False)
        if data.get("reset"):
            return f"{data.get('id')} -> {data.get('value')}"
        return f"{data.get('id')} = {data.get('value')}"
    if event.type == "document.opened":
        return f"{data.get('path')} ({data.get('width')}x{data.get('height')})"
    if event.type == "image.exported":
        return str(data.get("path"))
    if event.type in ("history.undo", "history.redo"):
        return str(data.get("label"))
    return json.dumps(data, ensure_ascii=False)[:180] if data else ""
