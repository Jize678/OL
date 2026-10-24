"""主窗口。

这里有一条硬规矩：**界面上的任何操作都通过 self._send(...) 发命令**，
不允许直接改 session.document 里的值。这样界面、HTTP API、MCP 三条路
永远是同一套行为，agent 也永远不会遇到「界面上有、接口里没有」的功能。
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..api.server import read_endpoint
from ..core.adjustments import registry
from ..core.color import BAND_ATTRIBUTE_LABELS, BAND_LABELS, BAND_COLORS
from ..core.invoker import MainThreadInvoker
from ..core.session import Session
from .canvas import EmptyStateOverlay, ImageCanvas
from .curve_editor import CurveEditor
from .render_worker import PreviewRenderer, RenderPayload
from .theme import COLORS
from .widgets import AdjustmentRow, AgentConsole, CollapsibleSection, HistogramWidget

IMAGE_FILTER = ("照片 (*.jpg *.jpeg *.jpe *.png *.bmp *.tif *.tiff *.webp *.gif);;"
                "所有文件 (*)")

DRAG_PREVIEW_SIDE = 900   # 拖动滑块时的预览分辨率，追求跟手

HSL_GROUP = "HSL 混色器"
CURVE_GROUP = "曲线"
# 默认展开的组：最常用的先露出来，其余收起，面板不至于一屏塞不下
DEFAULT_EXPANDED_GROUPS = ("白平衡", "基本")
HSL_MODES = (("hue", "色相"), ("sat", "饱和度"), ("lum", "明亮度"))

# 曲线预设：人点一下就能得到常见效果，也顺便告诉 agent 这些形状长什么样
CURVE_PRESETS = (
    ("S 形加对比", [[0.0, 0.0], [0.25, 0.19], [0.75, 0.81], [1.0, 1.0]]),
    ("提亮中间调", [[0.0, 0.0], [0.5, 0.58], [1.0, 1.0]]),
    ("褪色（抬黑）", [[0.0, 0.08], [1.0, 0.94]]),
)


class MainWindow(QMainWindow):
    def __init__(self, session: Session, api_server, invoker: MainThreadInvoker,
                 parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.api = api_server
        self.invoker = invoker

        self.setWindowTitle(f"{APP_NAME} {__version__} — agent 友好型照片处理")
        self.resize(1440, 900)
        self.setAcceptDrops(True)

        self._draft_params: dict | None = None
        self._rows: dict[str, AdjustmentRow] = {}
        self._sections: dict[str, CollapsibleSection] = {}
        self._current_side = session.settings.preview_max_side
        self._last_histogram: dict = {}

        self.renderer = PreviewRenderer(self)
        self.renderer.rendered.connect(self._on_rendered)
        self.renderer.start()

        self._build_ui()
        self._build_actions()

        self.session.events.subscribe(self._on_event)
        self._refresh_api_status()
        self._sync_controls()

    # ==================================================================
    # 构建界面
    # ==================================================================
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_canvas())
        splitter.addWidget(self._build_side_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1040, 360])
        self.setCentralWidget(splitter)

        self.console = AgentConsole()
        dock = QDockWidget("Agent 控制台 — 谁在操作这个软件", self)
        dock.setObjectName("agent-console")
        dock.setWidget(self.console)
        dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.console_dock = dock

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel()
        status.addWidget(self.status_label, 1)

        self.dimension_label = QLabel("—")
        self.zoom_label = QLabel("—")
        self.command_count_label = QLabel("命令 0")
        self.api_indicator = QLabel()
        for widget in (self.dimension_label, self.zoom_label,
                       self.command_count_label, self.api_indicator):
            widget.setStyleSheet(f"color:{COLORS['text_dim']}; padding: 0 10px;")
            status.addPermanentWidget(widget)
        self._set_status("打开一张照片开始，或让 agent 通过 API 打开。")

    def _build_canvas(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = ImageCanvas()
        self.canvas.zoom_changed.connect(
            lambda z: self.zoom_label.setText(f"缩放 {z * 100:.0f}%")
        )
        self.canvas.file_dropped.connect(self.open_path)
        layout.addWidget(self.canvas)

        self.empty_overlay = EmptyStateOverlay(self.canvas)
        self.empty_overlay.setGeometry(self.canvas.geometry())
        self.empty_overlay.raise_()
        return container

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(320)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        # --- 照片信息 ---
        info_box = QGroupBox("照片信息")
        info_layout = QVBoxLayout(info_box)
        info_layout.setSpacing(4)
        self.file_label = QLabel("未打开照片")
        self.file_label.setStyleSheet("font-weight:600; font-size:13px;")
        self.file_label.setWordWrap(True)
        self.meta_label = QLabel("—")
        self.meta_label.setStyleSheet(f"color:{COLORS['text_dim']};")
        self.meta_label.setWordWrap(True)
        self.exif_label = QLabel("")
        self.exif_label.setStyleSheet(f"color:{COLORS['text_dim']};")
        self.exif_label.setWordWrap(True)
        info_layout.addWidget(self.file_label)
        info_layout.addWidget(self.meta_label)
        info_layout.addWidget(self.exif_label)
        outer.addWidget(info_box)

        # --- 直方图 ---
        hist_box = QGroupBox("直方图")
        hist_layout = QVBoxLayout(hist_box)
        hist_layout.setContentsMargins(6, 6, 6, 6)
        self.histogram = HistogramWidget()
        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet(f"color:{COLORS['text_dim']};")
        hist_layout.addWidget(self.histogram)
        hist_layout.addWidget(self.stats_label)
        outer.addWidget(hist_box)

        # --- 调整：面板完全由注册表生成，加参数不用改这里 ---
        adjust_box = QGroupBox(f"调整（{len(registry.all())} 项参数）")
        adjust_layout = QVBoxLayout(adjust_box)
        adjust_layout.setSpacing(6)
        for group in registry.groups():
            section = CollapsibleSection(group["id"], group["label"],
                                         expanded=group["id"] in DEFAULT_EXPANDED_GROUPS)
            section.reset_requested.connect(self._on_group_reset)
            self._sections[group["id"]] = section
            if group["id"] == HSL_GROUP:
                self._build_hsl_section(section)
            elif group["id"] == CURVE_GROUP:
                self._build_curve_section(section)
            else:
                for spec in registry.by_group(group["id"]):
                    self._add_adjustment_row(section, spec)
            adjust_layout.addWidget(section)

        adjust_layout.addWidget(_separator())

        reset_all = QPushButton("全部重置")
        reset_all.clicked.connect(lambda: self._send("adjustments.reset_all"))
        adjust_layout.addWidget(reset_all)
        outer.addWidget(adjust_box)

        # --- Agent 接口 ---
        agent_box = QGroupBox("Agent 接口")
        agent_layout = QVBoxLayout(agent_box)
        agent_layout.setSpacing(6)
        self.api_label = QLabel()
        self.api_label.setWordWrap(True)
        self.api_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        agent_layout.addWidget(self.api_label)

        buttons = QHBoxLayout()
        copy_url = QPushButton("复制地址")
        copy_url.clicked.connect(self._copy_api_url)
        copy_schema = QPushButton("复制 schema")
        copy_schema.clicked.connect(self._copy_schema_command)
        copy_mcp = QPushButton("复制 MCP 配置")
        copy_mcp.clicked.connect(self._copy_mcp_config)
        buttons.addWidget(copy_url)
        buttons.addWidget(copy_schema)
        buttons.addWidget(copy_mcp)
        agent_layout.addLayout(buttons)

        note = QLabel(
            "界面上的每一步操作都会变成一条命令，agent 发给 API 的也是同一条命令。"
            "所以 agent 能做到的事，你在界面上都能做；你在界面上做的事，agent 也看得见。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{COLORS['text_dim']};")
        agent_layout.addWidget(note)
        outer.addWidget(agent_box)
        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(panel)
        return scroll

    # ------------------------------------------------------------------
    # 调整面板的构件（全部由注册表驱动）
    # ------------------------------------------------------------------
    def _add_adjustment_row(self, section: CollapsibleSection, spec,
                            *, compact: bool = False) -> AdjustmentRow:
        row = AdjustmentRow(spec, compact=compact)
        row.preview.connect(self._on_preview_drag)
        row.committed.connect(self._on_commit)
        row.reset_requested.connect(self._on_reset)
        self._rows[spec.id] = row
        section.add_widget(row)
        return row

    def _build_hsl_section(self, section: CollapsibleSection) -> None:
        """HSL 混色器：8 个色带 x 3 个属性。

        24 个滑块全铺开会淹掉整个面板，所以一次只显示一个属性，
        和 Lightroom 的做法一样。隐藏的滑块照样跟着 agent 的命令同步。
        """
        picker = QHBoxLayout()
        picker.setSpacing(6)
        picker.addWidget(QLabel("调节"))
        self.hsl_mode = QComboBox()
        for attribute, label in HSL_MODES:
            self.hsl_mode.addItem(label, attribute)
        self.hsl_mode.setCurrentIndex(1)          # 饱和度最常用
        self.hsl_mode.currentIndexChanged.connect(self._on_hsl_mode_changed)
        picker.addWidget(self.hsl_mode, 1)
        section.add_layout(picker)

        for spec in registry.by_group(HSL_GROUP):
            _, band, attribute = spec.id.split("_")
            row = self._add_adjustment_row(section, spec, compact=True)
            row.name_label.setText(
                f'<span style="color:{BAND_COLORS[band]}">●</span> {BAND_LABELS[band]}')
            row.name_label.setToolTip(
                f"{BAND_LABELS[band]} · {BAND_ATTRIBUTE_LABELS[attribute]}\n"
                f"{spec.description}\n（双击复位）")
            row.setVisible(attribute == "sat")

    def _build_curve_section(self, section: CollapsibleSection) -> None:
        """曲线：一个通道选择器 + 一个可拖点的编辑器 + 几个常用形状。"""
        picker = QHBoxLayout()
        picker.setSpacing(6)
        picker.addWidget(QLabel("通道"))
        self.curve_combo = QComboBox()
        for spec in registry.by_group(CURVE_GROUP):
            self.curve_combo.addItem(spec.label, spec.id)
        self.curve_combo.currentIndexChanged.connect(self._on_curve_channel_changed)
        picker.addWidget(self.curve_combo, 1)
        section.add_layout(picker)

        self.curve_editor = CurveEditor()
        self.curve_editor.preview.connect(self._on_preview_drag)
        self.curve_editor.committed.connect(self._on_curve_commit)
        section.add_widget(self.curve_editor)

        hint = QLabel("单击空白处加点、拖动改形状、右键或双击删点。拖动中不写历史，松手才记一步。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{COLORS['text_dim']};")
        section.add_widget(hint)

        presets = QHBoxLayout()
        presets.setSpacing(4)
        for label, points in CURVE_PRESETS:
            button = QPushButton(label)
            button.setToolTip(f"直接套用这条曲线：{json.dumps(points)}")
            button.clicked.connect(
                lambda _checked=False, preset=points: self._apply_curve_preset(preset))
            presets.addWidget(button)
        section.add_layout(presets)

    # -- 曲线 / 分组交互 -----------------------------------------------------
    def _current_curve_id(self) -> str:
        if getattr(self, "curve_combo", None) is None:
            return "curve_rgb"
        return self.curve_combo.currentData() or "curve_rgb"

    def _on_curve_channel_changed(self, *_args) -> None:
        curve_id = self._current_curve_id()
        spec = registry.get(curve_id)
        document = self.session.document
        points = spec.default_value()
        if document is not None:
            points = document.params.get(curve_id) or spec.default_value()
        self.curve_editor.set_adjustment(curve_id, spec.curve_channel or "rgb", points)
        self.curve_editor.set_histogram(self._last_histogram)

    def _on_curve_commit(self, curve_id: str, points) -> None:
        self._draft_params = None
        self._send("curve.set_points", id=curve_id, points=points)

    def _on_curve_reset(self) -> None:
        if self.session.document is None:
            self._set_status("先打开一张照片", error=True)
            return
        self._send("adjustment.reset", id=self._current_curve_id())

    def _apply_curve_preset(self, points) -> None:
        if self.session.document is None:
            self._set_status("先打开一张照片", error=True)
            return
        self._send("curve.set_points", id=self._current_curve_id(), points=points)

    def _on_group_reset(self, group_id: str) -> None:
        """把一整组恢复默认。走 set_batch，所以只占一步历史。"""
        document = self.session.document
        if document is None:
            return
        changes = {
            spec.id: spec.default_value()
            for spec in registry.by_group(group_id)
            if not registry.is_default(spec.id, document.params.get(spec.id))
        }
        if not changes:
            self._set_status(f"{group_id}：本来就都是默认值")
            return
        self._send("adjustments.set_batch", adjustments=changes)

    def _on_hsl_mode_changed(self, *_args) -> None:
        attribute = self.hsl_mode.currentData()
        for spec in registry.by_group(HSL_GROUP):
            row = self._rows.get(spec.id)
            if row is not None:
                row.setVisible(spec.id.endswith("_" + attribute))
        self._set_status(f"HSL 混色器：正在调「{self.hsl_mode.currentText()}」")

    def _build_actions(self) -> None:
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self.action_open = QAction("打开照片", self)
        self.action_open.setShortcut(QKeySequence.Open)
        self.action_open.triggered.connect(self.action_open_image)

        self.action_save_recipe = QAction("保存配方", self)
        self.action_save_recipe.setShortcut(QKeySequence.Save)
        self.action_save_recipe.triggered.connect(lambda: self._send("recipe.save"))

        self.action_export = QAction("导出成品", self)
        self.action_export.setShortcut("Ctrl+E")
        self.action_export.triggered.connect(self.action_export_image)

        self.action_undo = QAction("撤销", self)
        self.action_undo.setShortcut(QKeySequence.Undo)
        self.action_undo.triggered.connect(lambda: self._send("history.undo"))

        self.action_redo = QAction("重做", self)
        self.action_redo.setShortcuts([QKeySequence.Redo, "Ctrl+Shift+Z"])
        self.action_redo.triggered.connect(lambda: self._send("history.redo"))

        self.action_fit = QAction("适应窗口", self)
        self.action_fit.setShortcut("Ctrl+0")
        self.action_fit.triggered.connect(self.canvas.fit_to_view)

        self.action_zoom100 = QAction("100%", self)
        self.action_zoom100.setShortcut("Ctrl+1")
        self.action_zoom100.triggered.connect(lambda: self.canvas.zoom_to(1.0))

        self.action_compare = QAction("对比原图", self)
        self.action_compare.setCheckable(True)
        self.action_compare.setShortcut("C")
        self.action_compare.toggled.connect(self.canvas.set_comparison_active)

        self.action_split = QAction("分屏对比", self)
        self.action_split.setCheckable(True)
        self.action_split.setShortcut("Ctrl+\\")
        self.action_split.toggled.connect(self._on_split_toggled)

        self.action_console = QAction("Agent 控制台", self)
        self.action_console.setCheckable(True)
        self.action_console.setChecked(True)
        self.action_console.toggled.connect(self.console_dock.setVisible)

        self.action_reset_all = QAction("重置全部调整", self)
        self.action_reset_all.setShortcut("Ctrl+R")
        self.action_reset_all.triggered.connect(lambda: self._send("adjustments.reset_all"))

        self.action_copy_schema = QAction("复制 schema 命令", self)
        self.action_copy_schema.triggered.connect(self._copy_schema_command)

        self.action_restart_api = QAction("重启 Agent 服务", self)
        self.action_restart_api.triggered.connect(self._restart_api)

        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save_recipe)
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = self.menuBar().addMenu("编辑(&E)")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_reset_all)

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.action_fit)
        view_menu.addAction(self.action_zoom100)
        view_menu.addSeparator()
        view_menu.addAction(self.action_compare)
        view_menu.addAction(self.action_split)
        view_menu.addSeparator()
        view_menu.addAction(self.action_console)

        agent_menu = self.menuBar().addMenu("Agent(&A)")
        agent_menu.addAction(self.action_copy_schema)
        agent_menu.addAction(self.action_restart_api)

        for item in (self.action_open, self.action_save_recipe, self.action_export, None,
                     self.action_undo, self.action_redo, None,
                     self.action_fit, self.action_zoom100, None,
                     self.action_compare, self.action_split, None,
                     self.action_console):
            if item is None:
                toolbar.addSeparator()
            else:
                toolbar.addAction(item)

    # ==================================================================
    # 命令通道
    # ==================================================================
    def _send(self, cmd: str, **params) -> dict:
        """唯一的写入通道：界面 -> 命令总线。"""
        response = self.session.execute({"cmd": cmd, "params": params, "source": "gui"})
        if not response.get("ok"):
            error = response.get("error") or {}
            self._set_status(f"操作失败：{error.get('message', '未知错误')}", error=True)
            if error.get("code") not in ("nothing_to_undo", "nothing_to_redo"):
                QMessageBox.warning(self, "操作失败",
                                    f"{error.get('message', '')}\n\n{error.get('hint', '') or ''}")
        return response

    # ==================================================================
    # 交互动作
    # ==================================================================
    def open_path(self, path: str) -> None:
        path = str(path).strip().strip('"')
        if path:
            self._send("document.open", path=path)

    def action_open_image(self) -> None:
        start_dir = self.session.settings.last_open_dir or ""
        path, _ = QFileDialog.getOpenFileName(self, "打开照片", start_dir, IMAGE_FILTER)
        if path:
            self.open_path(path)

    def action_export_image(self) -> None:
        document = self.session.document
        if document is None:
            QMessageBox.information(self, "还没有照片", "先打开一张照片再导出。")
            return
        suggested = str(document.path.with_name(
            f"{document.path.stem}{self.session.settings.export_suffix}{document.path.suffix}"
        ))
        path, _ = QFileDialog.getSaveFileName(self, "导出成品", suggested, IMAGE_FILTER)
        if path:
            self._send("export.image", path=path, overwrite=True)

    def _on_split_toggled(self, enabled: bool) -> None:
        if enabled and self.action_compare.isChecked():
            self.action_compare.setChecked(False)
        self.canvas.set_split_mode(enabled)
        if enabled:
            self._ensure_original_preview()

    def _on_preview_drag(self, adjustment_id: str, value) -> None:
        """拖动过程中：只更新画面，不写历史、不发命令。

        滑块和曲线共用这条路径：滑块给 float，曲线给控制点列表。
        """
        document = self.session.document
        if document is None:
            return
        params = dict(document.params)
        params[adjustment_id] = value
        self._draft_params = params
        self._current_side = min(self.session.settings.preview_max_side, DRAG_PREVIEW_SIDE)
        self.renderer.request("current", params, self._current_side)
        self._set_status(f"预览中：{adjustment_id} = {_describe_value(value)}")

    def _on_commit(self, adjustment_id: str, value: float) -> None:
        self._draft_params = None
        self._send("adjustment.set", id=adjustment_id, value=value)

    def _on_reset(self, adjustment_id: str) -> None:
        self._send("adjustment.reset", id=adjustment_id)

    # ==================================================================
    # 事件 -> 界面
    # ==================================================================
    def _on_event(self, event) -> None:
        self.console.append_event(event)
        self.command_count_label.setText(f"命令 {self.session.state_snapshot()['commands_run']}")

        if event.type == "document.opened":
            self._current_side = self.session.settings.preview_max_side
            self.renderer.set_document(self.session.document)
            self._sync_controls()
            self._request_render()
            self._ensure_original_preview()
            self.canvas.fit_to_view()
            document = self.session.document
            if document is not None:
                self._set_status(f"已打开 {document.filename}")
        elif event.type == "document.closed":
            self.renderer.set_document(None)
            self.canvas.set_image(None)
            self.canvas.set_comparison(None)
            self.histogram.clear()
            self._sync_controls()
        elif event.type in ("adjustment.changed", "history.undo", "history.redo"):
            if self._draft_params is None:      # 拖动中的中间态不用回灌
                self._sync_controls()
                self._current_side = self.session.settings.preview_max_side
                self._request_render()
        elif event.type == "image.exported":
            self._set_status(f"已导出：{event.data.get('path')}")
        elif event.type == "recipe.saved":
            self._set_status(f"配方已保存：{event.data.get('path')}")

    def _request_render(self) -> None:
        document = self.session.document
        if document is None:
            return
        params = self._draft_params or dict(document.params)
        self.renderer.request("current", params, self._current_side)

    def _ensure_original_preview(self) -> None:
        if self.session.document is None:
            return
        self.renderer.request("original", registry.default_params(), self._current_side)

    def _on_rendered(self, payload: RenderPayload) -> None:
        if self.session.document is None:
            return
        if payload.kind == "original":
            self.canvas.set_comparison(payload.image)
            return

        self.canvas.set_image(payload.image)
        if self.action_compare.isChecked() or self.action_split.isChecked():
            self._ensure_original_preview()
        self._last_histogram = payload.histogram
        if hasattr(self, "curve_editor"):
            self.curve_editor.set_histogram(payload.histogram)
        self.histogram.set_data(payload.histogram,
                                payload.stats.get("clipped_highlights_pct", 0.0),
                                payload.stats.get("clipped_shadows_pct", 0.0))
        self.stats_label.setText(
            f"平均亮度 {payload.stats.get('mean_luma', 0):.3f}　"
            f"高光削波 {payload.stats.get('clipped_highlights_pct', 0):.2f}%　"
            f"暗部削波 {payload.stats.get('clipped_shadows_pct', 0):.2f}%"
        )
        if self._draft_params is None:
            self._set_status("已更新预览")

    # ==================================================================
    # 状态同步
    # ==================================================================
    def _sync_controls(self) -> None:
        document = self.session.document
        has_document = document is not None

        self.action_save_recipe.setEnabled(has_document)
        self.action_export.setEnabled(has_document)
        self.action_compare.setEnabled(has_document)
        self.action_split.setEnabled(has_document)
        self.action_reset_all.setEnabled(has_document)
        self.empty_overlay.setVisible(not has_document)

        if not has_document:
            self.file_label.setText("未打开照片")
            self.meta_label.setText("用 Ctrl+O 打开，或让 agent 调 document.open")
            self.exif_label.setText("")
            self.dimension_label.setText("—")
            for row in self._rows.values():
                row.set_value(row.spec.default_value())
            for section in self._sections.values():
                section.set_active_count(0)
            self.action_undo.setEnabled(False)
            self.action_redo.setEnabled(False)
            return

        state = self.session.state_snapshot()
        self.action_undo.setEnabled(state["can_undo"])
        self.action_redo.setEnabled(state["can_redo"])

        params = document.params
        for adjustment_id, row in self._rows.items():
            row.set_value(params.get(adjustment_id, row.spec.default_value()))

        # 分组标题上的「已调 N 项」+ 曲线编辑器当前内容
        for group_id, section in self._sections.items():
            count = sum(1 for spec in registry.by_group(group_id)
                        if not registry.is_default(spec.id, params.get(spec.id)))
            section.set_active_count(count)
        if hasattr(self, "curve_editor"):
            curve_id = self._current_curve_id()
            spec = registry.get(curve_id)
            self.curve_editor.set_adjustment(
                curve_id, spec.curve_channel or "rgb",
                params.get(curve_id) or spec.default_value())

        size_mb = document.meta.get("size_bytes", 0) / 1024 / 1024
        self.file_label.setText(document.filename)
        self.meta_label.setText(
            f"{document.width} × {document.height}　"
            f"{document.width * document.height / 1e6:.1f} MP\n"
            f"{document.meta.get('format', '')}　{size_mb:.1f} MB\n"
            f"{document.path}"
        )
        exif = document.meta.get("exif") or {}
        pieces = []
        if exif.get("make") or exif.get("model"):
            pieces.append(f"{exif.get('make', '')} {exif.get('model', '')}".strip())
        if exif.get("exposure_time"):
            pieces.append(str(exif["exposure_time"]))
        if exif.get("f_number"):
            pieces.append(f"f/{exif['f_number']:g}")
        if exif.get("iso"):
            pieces.append(f"ISO {exif['iso']}")
        self.exif_label.setText("　".join(pieces))
        self.dimension_label.setText(f"{document.width} × {document.height}")

    def _refresh_api_status(self) -> None:
        if self.api is not None and self.api.running:
            endpoint = read_endpoint() or {}
            self.api_indicator.setText(f"● Agent 服务 {self.api.base_url}")
            self.api_indicator.setStyleSheet(
                f"color:{COLORS['agent']}; padding: 0 10px; font-weight: 600;"
            )
            self.api_label.setText(
                f"运行中：{self.api.base_url}\n"
                f"自述文档：GET /schema\n"
                f"执行命令：POST /command\n"
                f"端口记录：{endpoint.get('base_url', '—')}"
            )
        else:
            self.api_indicator.setText("● Agent 服务未启动")
            self.api_indicator.setStyleSheet(
                f"color:{COLORS['danger']}; padding: 0 10px; font-weight: 600;"
            )
            self.api_label.setText("未启动。用「Agent → 重启 Agent 服务」打开。")

    def _set_status(self, text: str, error: bool = False) -> None:
        color = COLORS["danger"] if error else COLORS["text_dim"]
        self.status_label.setStyleSheet(f"color:{color}; padding: 0 10px;")
        self.status_label.setText(text)

    # ==================================================================
    # Agent 相关小工具
    # ==================================================================
    def _copy_api_url(self) -> None:
        url = self.api.base_url if self.api else "http://127.0.0.1:8765"
        QGuiApplication.clipboard().setText(url)
        self._set_status(f"已复制接口地址：{url}")

    def _copy_schema_command(self) -> None:
        url = self.api.base_url if self.api else "http://127.0.0.1:8765"
        QGuiApplication.clipboard().setText(f"curl -s {url}/schema")
        self._set_status("已复制：agent 用它可以问到这个软件会什么")

    def _copy_mcp_config(self) -> None:
        project = str(Path(__file__).resolve().parents[2])
        config = {
            "mcpServers": {
                "openlight": {
                    "command": "python",
                    "args": ["-m", "openlight.mcp"],
                    "cwd": project,
                }
            }
        }
        QGuiApplication.clipboard().setText(json.dumps(config, ensure_ascii=False, indent=2))
        self._set_status("已复制 MCP 配置，粘到 agent 的配置文件里即可")

    def _restart_api(self) -> None:
        if self.api is None:
            return
        self.api.stop()
        try:
            self.api.start()
            self._set_status(f"Agent 服务已启动：{self.api.base_url}")
        except Exception as exc:
            self._set_status(f"启动失败：{exc}", error=True)
        self._refresh_api_status()

    # ==================================================================
    # 生命周期
    # ==================================================================
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "empty_overlay"):
            self.empty_overlay.setGeometry(self.canvas.rect())

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.open_path(url.toLocalFile())
                break

    def closeEvent(self, event):  # noqa: N802
        self.session.events.unsubscribe(self._on_event)
        self.renderer.shutdown()
        super().closeEvent(event)


def _describe_value(value) -> str:
    """状态栏里怎么显示当前值：滑块是数字，曲线是控制点个数。"""
    if isinstance(value, (list, tuple)):
        return f"曲线 {len(value)} 个控制点"
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return str(value)


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color:{COLORS['border']};")
    return line
