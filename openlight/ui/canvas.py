"""画布：显示照片、缩放、平移、对比原图。

界面线程只负责画 QImage，真正的像素运算发生在后台渲染线程里，
所以拖动滑块时窗口不会卡住。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QSizePolicy,
)

from .theme import COLORS


class ImageCanvas(QGraphicsView):
    zoom_changed = Signal(float)
    file_dropped = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setBackgroundBrush(QColor(COLORS["canvas"]))
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self._item.setZValue(1)
        self._scene.addItem(self._item)

        self._comparison_item = QGraphicsPixmapItem()
        self._comparison_item.setTransformationMode(Qt.SmoothTransformation)
        self._comparison_item.setZValue(0)
        self._scene.addItem(self._comparison_item)

        self._pixmap = QPixmap()            # 当前参数下的画面
        self._original_pixmap = QPixmap()   # 全默认参数下的画面
        self._panning = False
        self._pan_origin = QPointF()
        self._comparison = False
        self._split_mode = False       # 左右分屏对比
        self._empty_hint = "把照片拖进来，或按 Ctrl+O 打开"
        self._zoom = 1.0

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def set_image(self, image: QImage | None) -> None:
        if image is None:
            self._pixmap = QPixmap()
            self._item.setPixmap(QPixmap())
            self._scene.setSceneRect(0, 0, 0, 0)
            self._apply_display()
            self.update()
            return
        self._pixmap = QPixmap.fromImage(image)
        scene_rect = self._pixmap.rect()
        if not self._original_pixmap.isNull():
            scene_rect = scene_rect.united(self._original_pixmap.rect())
        self._scene.setSceneRect(scene_rect)
        self._apply_display()
        self.update()

    def set_comparison(self, image: QImage | None) -> None:
        self._original_pixmap = QPixmap.fromImage(image) if image is not None else QPixmap()
        self._apply_display()
        self.update()

    def has_image(self) -> bool:
        return not self._pixmap.isNull()

    def image_size(self) -> tuple[int, int]:
        return self._pixmap.width(), self._pixmap.height()

    # ------------------------------------------------------------------
    # 对比
    # ------------------------------------------------------------------
    def set_comparison_active(self, active: bool) -> None:
        self._comparison = active
        self._apply_display()
        self.update()

    def set_split_mode(self, enabled: bool) -> None:
        self._split_mode = enabled
        self._apply_display()
        self.update()

    def _apply_display(self) -> None:
        """三种显示模式：只看当前 / 按住看原图 / 左右分屏对比。"""
        if self._pixmap.isNull():
            self._item.setPixmap(QPixmap())
            self._comparison_item.setPixmap(QPixmap())
            self._item.setVisible(False)
            self._comparison_item.setVisible(False)
            return

        has_original = not self._original_pixmap.isNull()
        self._item.setPos(0, 0)
        self._comparison_item.setPos(0, 0)

        if self._split_mode and has_original:
            half = self._pixmap.width() // 2
            self._comparison_item.setPixmap(
                self._original_pixmap.copy(0, 0, half, self._original_pixmap.height())
            )
            self._item.setPixmap(
                self._pixmap.copy(half, 0, self._pixmap.width() - half, self._pixmap.height())
            )
            self._item.setPos(half, 0)
            self._item.setVisible(True)
            self._comparison_item.setVisible(True)
            return

        self._item.setPixmap(self._pixmap)
        self._comparison_item.setPixmap(self._original_pixmap if has_original else QPixmap())
        if self._comparison and has_original:
            self._item.setVisible(False)
            self._comparison_item.setVisible(True)
        else:
            self._item.setVisible(True)
            self._comparison_item.setVisible(False)

    # ------------------------------------------------------------------
    # 缩放
    # ------------------------------------------------------------------
    def fit_to_view(self) -> None:
        if self._pixmap.isNull():
            return
        self.resetTransform()
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)

    def zoom_to(self, factor: float) -> None:
        if self._pixmap.isNull():
            return
        self.resetTransform()
        self.scale(factor, factor)
        self._zoom = factor
        self.zoom_changed.emit(self._zoom)

    def zoom_in(self) -> None:
        self._nudge_zoom(1.25)

    def zoom_out(self) -> None:
        self._nudge_zoom(1 / 1.25)

    def _nudge_zoom(self, ratio: float) -> None:
        if self._pixmap.isNull():
            return
        new_zoom = max(0.05, min(8.0, self._zoom * ratio))
        self.scale(new_zoom / self._zoom, new_zoom / self._zoom)
        self._zoom = new_zoom
        self.zoom_changed.emit(self._zoom)

    def wheelEvent(self, event):  # noqa: N802
        if self._pixmap.isNull():
            return
        delta = event.angleDelta().y()
        if delta:
            self._nudge_zoom(1.15 if delta > 0 else 1 / 1.15)
        event.accept()

    # ------------------------------------------------------------------
    # 平移
    # ------------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() in (Qt.MiddleButton, Qt.LeftButton) and self._pixmap.width():
            self._panning = True
            self._pan_origin = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._panning:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.fit_to_view()
        event.accept()

    # ------------------------------------------------------------------
    # 拖拽打开
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.file_dropped.emit(url.toLocalFile())
                break
        event.acceptProposedAction()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def drawForeground(self, painter: QPainter, rect) -> None:  # noqa: N802
        if self._split_mode and not self._original_pixmap.isNull() and not self._pixmap.isNull():
            painter.setPen(QPen(QColor(COLORS["accent"]), 1 / max(self._zoom, 0.01)))
            x = self._pixmap.width() / 2
            painter.drawLine(QPointF(x, 0), QPointF(x, self._pixmap.height()))
            font = painter.font()
            font.setPointSizeF(max(9, 11 / max(self._zoom, 0.2)))
            painter.setFont(font)
            painter.setPen(QColor(COLORS["human"]))
            painter.drawText(QPointF(x * 0.15, self._pixmap.height() * 0.05), "原图")
            painter.setPen(QColor(COLORS["accent"]))
            painter.drawText(QPointF(x * 1.1, self._pixmap.height() * 0.05), "当前")

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if self._pixmap.width() and self._zoom <= 0.01:
            self.fit_to_view()


class EmptyStateOverlay(QLabel):
    """空状态提示，叠在画布上。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            f"color:{COLORS['text_dim']}; font-size:14px; background:transparent;"
        )
        self.setText("把照片拖到这里\n\n或按 Ctrl+O 打开\n\n也可以让 agent 通过 API 打开：\n"
                     "document.open {path}")
