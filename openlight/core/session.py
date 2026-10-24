"""命令总线：整个软件唯一的「写入通道」。

    GUI ─┐
         ├─► Session.execute(command) ─► 文档 / 引擎 / 历史 ─► 事件日志 ─► 订阅者
    API ─┘

任何状态变更都必须经过这里。这样做的直接好处：
- agent 能做的 = 人能做的，不存在「只有鼠标能点到的功能」；
- 每条命令都进历史和事件日志，可审计、可回放、可撤销；
- 新增功能只要加一条命令，两个入口同时获得。
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from pathlib import Path

from PIL import Image

from . import engine
from .adjustments import registry
from .document import PhotoDocument, open_document
from .errors import OpenLightError, bad_request, no_document, not_found, unknown_command
from .events import EventLog
from .history import History

IMAGE_FORMATS = {
    "jpg": ("JPEG", {"quality": 95, "subsampling": 0}),
    "jpeg": ("JPEG", {"quality": 95, "subsampling": 0}),
    "png": ("PNG", {"compress_level": 6}),
    "tif": ("TIFF", {}),
    "tiff": ("TIFF", {}),
    "webp": ("WEBP", {"quality": 95, "method": 5}),
    "bmp": ("BMP", {}),
}

# 兼容写法：agent 可能会用 schema.get 之类的别名叫同一个命令。
_ALIASES = {
    "schema.get": "app.schema",
    "schema": "app.schema",
    "info": "app.info",
    "open": "document.open",
    "close": "document.close",
    "state": "document.state",
    "set_adjustment": "adjustment.set",
    "reset_adjustment": "adjustment.reset",
    "undo": "history.undo",
    "redo": "history.redo",
    "preview": "preview.get",
    "export": "export.image",
}


def _label_for(spec, value) -> str:
    """历史记录里的可读标签。曲线的值是控制点列表，不能按数字格式化。"""
    if spec.kind == "curve":
        return "%s曲线（%d 点）" % (spec.label, len(value))
    if spec.precision:
        text = ("%+.*f" % (spec.precision, float(value))).strip()
    else:
        text = "%+d" % round(float(value))
    return ("%s %s %s" % (spec.label, text, spec.unit)).strip()


def _points_of(value) -> list[list[float]] | None:
    """把控制点整理成 [[x, y], ...]；格式不对返回 None。"""
    parsed: list[list[float]] = []
    try:
        for item in value:
            if isinstance(item, dict):
                parsed.append([float(item["x"]), float(item["y"])])
            else:
                parsed.append([float(item[0]), float(item[1])])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return parsed


def _was_clamped(requested, stored) -> bool:
    """请求值和最终存下来的值是否不同（超范围被夹住 / 控制点被规范化）。"""
    if isinstance(stored, list):
        asked = _points_of(requested) if isinstance(requested, (list, tuple)) else None
        if asked is None:
            return True
        return asked != _points_of(stored)
    try:
        return abs(float(requested) - float(stored)) > 1e-9
    except (TypeError, ValueError):
        return True


class Session:
    """持有当前文档、历史和事件日志的命令执行器。"""

    def __init__(self, settings=None) -> None:
        from ..config import Settings

        self.settings = settings or Settings.load()
        self.events = EventLog()
        self.history = History()
        self.document: PhotoDocument | None = None
        self._preview_cache: dict[tuple, engine.RenderResult] = {}
        self._lock = threading.RLock()
        self._last_preview: dict | None = None
        self._commands_run = 0
        self._last_command: dict | None = None
        self._handlers = {
            "app.info": self._cmd_app_info,
            "app.schema": self._cmd_app_schema,
            "app.ping": self._cmd_app_ping,
            "document.open": self._cmd_document_open,
            "document.close": self._cmd_document_close,
            "document.state": self._cmd_document_state,
            "adjustment.set": self._cmd_adjustment_set,
            "adjustment.reset": self._cmd_adjustment_reset,
            "curve.set_points": self._cmd_curve_set_points,
            "adjustments.set_batch": self._cmd_adjustments_batch,
            "adjustments.reset_all": self._cmd_adjustments_reset_all,
            "history.undo": self._cmd_history_undo,
            "history.redo": self._cmd_history_redo,
            "history.state": self._cmd_history_state,
            "preview.get": self._cmd_preview_get,
            "preview.save": self._cmd_preview_save,
            "export.image": self._cmd_export_image,
            "recipe.save": self._cmd_recipe_save,
            "recipe.reload": self._cmd_recipe_reload,
            "events.list": self._cmd_events_list,
        }

    # ------------------------------------------------------------------
    # 对外唯一入口
    # ------------------------------------------------------------------
    def execute(self, request: dict) -> dict:
        """执行一条命令，永远返回结构化结果，不抛异常。"""
        if not isinstance(request, dict):
            return self._failure("bad_request", "命令必须是一个 JSON 对象。", None, None,
                                 "形如 {\"cmd\": \"document.state\"}。")

        command = request.get("cmd") or request.get("command")
        params = request.get("params") or {}
        source = request.get("source") or "agent"
        request_id = request.get("request_id")

        if not isinstance(command, str) or not command:
            return self._failure(
                "bad_request", "缺少 cmd 字段。", request_id, None,
                "形如 {\"cmd\": \"document.state\"}。",
            )
        if not isinstance(params, dict):
            return self._failure("bad_request", "params 必须是对象。", request_id, command)

        handler = self._handlers.get(command)
        if handler is None:
            canonical = _ALIASES.get(command)
            if canonical:
                command = canonical
                handler = self._handlers.get(canonical)
        if handler is None:
            return self._failure(
                "unknown_command", f"未知命令：{command}", request_id, command,
                "调用 app.schema 获取全部可用命令。",
            )

        started = time.perf_counter()
        with self._lock:
            try:
                result = handler(params, source)
            except OpenLightError as exc:
                return self._failure(exc.code, exc.message, request_id, command, exc.hint)
            except Exception as exc:  # 兜底，别让异常穿透到网络层
                return self._failure(
                    "internal_error", f"{type(exc).__name__}: {exc}", request_id, command,
                    "这是软件内部错误，请把完整信息反馈给开发者。",
                )
            self._commands_run += 1
            self._last_command = {
                "cmd": command,
                "params": params,
                "source": source,
                "ts": time.time(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        return {
            "ok": True,
            "request_id": request_id,
            "cmd": command,
            "result": result,
            "state": self.state_snapshot(),
            "error": None,
        }

    def _failure(self, code, message, request_id, command, hint=None) -> dict:
        with self._lock:
            snapshot = self.state_snapshot()
        return {
            "ok": False,
            "request_id": request_id,
            "cmd": command,
            "result": None,
            "state": snapshot,
            "error": {"code": code, "message": message, "hint": hint},
        }

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def state_snapshot(self) -> dict:
        return {
            "document": self.document.to_dict() if self.document else None,
            "can_undo": self.history.can_undo,
            "can_redo": self.history.can_redo,
            "history_length": len(self.history.entries()),
            "last_event_seq": self.events.last_seq,
            "commands_run": self._commands_run,
            "last_command": self._last_command,
            "last_preview": self._last_preview,
        }

    # ------------------------------------------------------------------
    # app.*
    # ------------------------------------------------------------------
    def _cmd_app_ping(self, params: dict, source: str) -> dict:
        return {"pong": True, "ts": time.time()}

    def _cmd_app_info(self, params: dict, source: str) -> dict:
        from .. import APP_NAME, __version__
        from ..api.server import describe_service

        return {
            "name": APP_NAME,
            "version": __version__,
            "kind": "photo-editor",
            "capabilities": [
                "non-destructive editing",
                "single-command-bus (GUI 与 agent 等价)",
                "parametric adjustments (42 个参数 / 6 个面板)",
                "point curves (RGB + R/G/B 通道)",
                "HSL mixer (8 色带 x 色相/饱和度/明亮度)",
                "undo/redo",
                "preview rendering + 画面统计",
                "export",
                "sidecar recipe",
            ],
            "interface": describe_service(self),
            "hint": "先调用 app.schema，它会返回所有命令、参数范围和用法示例。",
        }

    def _cmd_app_schema(self, params: dict, source: str) -> dict:
        from ..api.catalog import command_catalog

        return {
            "commands": command_catalog(),
            "adjustments": [spec.to_dict() for spec in registry.all()],
            "groups": registry.groups(),
            "parameter_count": len(registry.all()),
            "notes": [
                "所有命令都通过同一入口执行：POST /command {\"cmd\": ..., \"params\": {...}}",
                "命令返回里的 state 字段是执行后的完整状态快照。",
                "失败时 ok=false，error.code 可用于程序化判断。",
            ],
        }

    # ------------------------------------------------------------------
    # document.*
    # ------------------------------------------------------------------
    def _cmd_document_open(self, params: dict, source: str) -> dict:
        path = params.get("path") or params.get("file")
        if not path:
            raise bad_request("document.open 需要 path 参数。",
                              "例：{\"cmd\":\"document.open\",\"params\":{\"path\":\"D:\\\\photo.jpg\"}}")
        document = open_document(path, params=params.get("adjustments"))
        self.document = document
        self._preview_cache.clear()
        self.history.reset(document.params, label=f"打开 {document.filename}")
        self.settings.last_open_dir = str(document.path.parent)
        self.events.emit(
            "document.opened",
            {"path": str(document.path), "width": document.width, "height": document.height,
             "adjustments": dict(document.params)},
            source,
        )
        return {"document": document.to_dict()}

    def _cmd_document_close(self, params: dict, source: str) -> dict:
        if self.document is not None:
            self.events.emit("document.closed", {"path": str(self.document.path)}, source)
        self.document = None
        self._preview_cache.clear()
        self.history = History()
        return {"closed": True}

    def _cmd_document_state(self, params: dict, source: str) -> dict:
        if self.document is None:
            return {"document": None, "message": "当前没有打开的照片。"}
        return {"document": self.document.to_dict()}

    # ------------------------------------------------------------------
    # adjustment(s).*
    # ------------------------------------------------------------------
    def _require_document(self) -> PhotoDocument:
        if self.document is None:
            raise no_document()
        return self.document

    def _require_spec(self, adjustment_id: str):
        if not registry.has(adjustment_id):
            raise OpenLightError(
                "unknown_adjustment",
                "没有名为 %s 的调整项。" % adjustment_id,
                "当前可用：" + "、".join(registry.ids()),
            )
        return registry.get(adjustment_id)

    def _cmd_curve_set_points(self, params: dict, source: str) -> dict:
        """设置某条点曲线的控制点（曲线的专用入口，等价于 adjustment.set）。"""
        document = self._require_document()
        adjustment_id = params.get("id") or params.get("curve") or "curve_rgb"
        spec = self._require_spec(adjustment_id)
        if spec.kind != "curve":
            raise OpenLightError(
                "not_a_curve",
                "%s 不是曲线参数，它是标量。" % adjustment_id,
                "曲线参数：" + "、".join(s.id for s in registry.all() if s.kind == "curve"),
            )
        if "points" not in params:
            raise bad_request(
                "缺少 points。",
                "例：{\"cmd\":\"curve.set_points\",\"params\":"
                "{\"id\":\"curve_rgb\",\"points\":[[0,0],[0.5,0.6],[1,1]]}}",
            )
        try:
            points = registry.coerce(adjustment_id, params["points"])
        except (TypeError, ValueError) as exc:
            raise bad_request("控制点不合法：%s" % exc,
                              "格式 [[x, y], ...]，x 与 y 在 0~1，x 必须递增。")
        document.set_param(adjustment_id, points)
        self._preview_cache.clear()
        self.history.push(document.params, label=_label_for(spec, points), source=source)
        self.events.emit(
            "adjustment.changed",
            {"id": adjustment_id, "value": points, "curve": True,
             "adjustments": dict(document.params)},
            source,
        )
        return {"id": adjustment_id, "points": points,
                "parameters": dict(document.params)}

    def _cmd_adjustment_set(self, params: dict, source: str) -> dict:
        document = self._require_document()
        adjustment_id = params.get("id") or params.get("adjustment")
        if not adjustment_id:
            raise bad_request("缺少 id，无法确定要调整哪一项。",
                              "例：{\"cmd\":\"adjustment.set\",\"params\":{\"id\":\"exposure\",\"value\":0.5}}")
        spec = self._require_spec(adjustment_id)
        if "value" not in params:
            raise bad_request(
                "缺少 value。",
                "标量参数传数字（如 0.5），曲线参数传控制点（如 [[0,0],[1,1]]）。",
            )
        try:
            value = registry.coerce(adjustment_id, params["value"])
        except (TypeError, ValueError) as exc:
            raise bad_request(
                "调整项 %s 的取值不合法：%s" % (adjustment_id, exc),
                "标量参数传数字；曲线参数传 [[x, y], ...] 且 x 必须递增。",
            )
        document.set_param(adjustment_id, value)
        self._preview_cache.clear()
        clamped = _was_clamped(params["value"], value)
        self.history.push(document.params, label=_label_for(spec, value), source=source)
        self.events.emit(
            "adjustment.changed",
            {
                "id": adjustment_id,
                "value": document.params[adjustment_id],
                "requested": params["value"],
                "clamped": clamped,
                "adjustments": dict(document.params),
            },
            source,
        )
        return {
            "id": adjustment_id,
            "value": document.params[adjustment_id],
            "clamped": clamped,
            "parameters": dict(document.params),
        }

    def _cmd_adjustment_reset(self, params: dict, source: str) -> dict:
        document = self._require_document()
        adjustment_id = params.get("id") or params.get("adjustment")
        if not adjustment_id or not registry.has(adjustment_id):
            raise OpenLightError(
                "unknown_adjustment", f"没有名为 {adjustment_id} 的调整项。",
                "当前可用：" + "、".join(s.id for s in registry.all()),
            )
        document.reset_param(adjustment_id)
        self._preview_cache.clear()
        spec = registry.get(adjustment_id)
        self.history.push(document.params, label=f"重置 {spec.label}", source=source)
        self.events.emit("adjustment.changed",
                         {"id": adjustment_id, "value": document.params[adjustment_id],
                          "reset": True, "adjustments": dict(document.params)}, source)
        return {"id": adjustment_id, "value": document.params[adjustment_id]}

    def _cmd_adjustments_batch(self, params: dict, source: str) -> dict:
        document = self._require_document()
        adjustments = params.get("adjustments") or params.get("params")
        if not isinstance(adjustments, dict) or not adjustments:
            raise bad_request(
                "需要 adjustments 对象，例如 {\"exposure\": 0.6}。",
                "一次可以设置多个调整项，但只会记住一个历史步骤。",
            )
        applied: dict = {}
        for key, value in adjustments.items():
            self._require_spec(key)
            try:
                coerced = registry.coerce(key, value)
            except (TypeError, ValueError) as exc:
                raise bad_request("调整项 %s 的取值不合法：%s" % (key, exc),
                                  "标量参数传数字；曲线参数传 [[x, y], ...]。")
            document.set_param(key, coerced)
            applied[key] = document.params[key]
        self._preview_cache.clear()
        label = "、".join(_label_for(registry.get(key), value)
                          for key, value in applied.items())
        self.history.push(document.params, label=label, source=source)
        self.events.emit("adjustment.changed",
                         {"batch": True, "adjustments": dict(document.params)}, source)
        return {"applied": applied, "parameters": dict(document.params)}

    def _cmd_adjustments_reset_all(self, params: dict, source: str) -> dict:
        document = self._require_document()
        document.reset_all()
        self._preview_cache.clear()
        self.history.push(document.params, label="全部重置", source=source)
        self.events.emit("adjustment.changed",
                         {"reset_all": True, "adjustments": dict(document.params)}, source)
        return {"parameters": dict(document.params)}

    # ------------------------------------------------------------------
    # history.*
    # ------------------------------------------------------------------
    def _cmd_history_undo(self, params: dict, source: str) -> dict:
        self._require_document()
        entry = self.history.undo()
        if entry is None:
            raise OpenLightError("nothing_to_undo", "没有可撤销的步骤了。")
        self.document.params = dict(entry.params)
        self._preview_cache.clear()
        self.events.emit("history.undo", {"label": entry.label,
                                          "adjustments": dict(entry.params)}, source)
        return {"restored": entry.to_dict(), "parameters": dict(entry.params)}

    def _cmd_history_redo(self, params: dict, source: str) -> dict:
        self._require_document()
        entry = self.history.redo()
        if entry is None:
            raise OpenLightError("nothing_to_redo", "没有可重做的步骤了。")
        self.document.params = dict(entry.params)
        self._preview_cache.clear()
        self.events.emit("history.redo", {"label": entry.label,
                                          "adjustments": dict(entry.params)}, source)
        return {"restored": entry.to_dict(), "parameters": dict(entry.params)}

    def _cmd_history_state(self, params: dict, source: str) -> dict:
        return self.history.to_dict()

    # ------------------------------------------------------------------
    # preview.*
    # ------------------------------------------------------------------
    def _render_preview(self, max_side: int) -> engine.RenderResult:
        document = self._require_document()
        # 曲线参数是嵌套列表，不能直接放进元组当 key，用规范化 JSON 串代替
        key = (document.id, json.dumps(document.params, sort_keys=True), max_side)
        cached = self._preview_cache.get(key)
        if cached is not None:
            return cached
        result = document.render_preview(max_side)
        if len(self._preview_cache) > 12:
            self._preview_cache.clear()
        self._preview_cache[key] = result
        self._last_preview = {
            "max_side": max_side,
            "width": result.image.width,
            "height": result.image.height,
            "stats": result.stats.to_dict(),
            "ts": time.time(),
        }
        return result

    def _cmd_preview_get(self, params: dict, source: str) -> dict:
        max_side = int(params.get("max_side") or params.get("maxSide")
                       or self.settings.preview_max_side)
        image_format = str(params.get("format") or "png").lower()
        result = self._render_preview(max_side)
        buffer = io.BytesIO()
        if image_format in ("jpg", "jpeg"):
            result.image.save(buffer, format="JPEG", quality=90)
            mime = "image/jpeg"
        else:
            result.image.save(buffer, format="PNG")
            mime = "image/png"
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        return {
            "mime": mime,
            "width": result.image.width,
            "height": result.image.height,
            "max_side": max_side,
            "encoding": "base64",
            "data_base64": payload,
            "bytes": len(buffer.getvalue()),
            "stats": result.stats.to_dict(),
        }

    def _cmd_preview_save(self, params: dict, source: str) -> dict:
        document = self._require_document()
        max_side = int(params.get("max_side") or self.settings.preview_max_side)
        result = self._render_preview(max_side)
        target = params.get("path")
        if target:
            target_path = Path(target).expanduser()
        else:
            target_path = document.path.with_name(
                f"{document.path.stem}.preview.png"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(target_path, format="PNG")
        return {
            "path": str(target_path),
            "width": result.image.width,
            "height": result.image.height,
            "stats": result.stats.to_dict(),
        }

    # ------------------------------------------------------------------
    # export.*
    # ------------------------------------------------------------------
    def _cmd_export_image(self, params: dict, source: str) -> dict:
        document = self._require_document()
        max_side = params.get("max_side")

        target = params.get("path")
        if target:
            target_path = Path(target).expanduser()
        else:
            suffix = document.path.suffix.lower().lstrip(".")
            target_path = document.path.with_name(
                f"{document.path.stem}{self.settings.export_suffix}.{suffix}"
            )

        overwrite = bool(params.get("overwrite", False))
        if target_path.exists() and not overwrite:
            raise OpenLightError(
                "file_exists",
                f"目标文件已存在：{target_path}",
                "换一个 path，或在命令里加 \"overwrite\": true。",
            )
        if target_path.resolve() == document.path.resolve():
            raise OpenLightError(
                "would_overwrite_source",
                "拒绝覆盖原始照片。",
                "换一个输出路径（默认会写成 xxx_openlight.jpg）。",
            )

        image_format = str(params.get("format") or target_path.suffix.lstrip(".") or "jpg").lower()
        if image_format not in IMAGE_FORMATS:
            raise bad_request(
                f"不支持导出为 {image_format}。", "支持：" + "、".join(sorted(IMAGE_FORMATS))
            )
        pil_format, save_options = IMAGE_FORMATS[image_format]
        save_options = dict(save_options)
        if "quality" in params and "quality" in save_options:
            save_options["quality"] = int(params["quality"])
        elif "quality" in save_options:
            save_options["quality"] = self.settings.jpeg_quality

        result = engine.engine.render(document.image, document.params, max_side=max_side)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(target_path, format=pil_format, **save_options)
        self.events.emit(
            "image.exported",
            {"path": str(target_path), "adjustments": dict(document.params),
             "width": result.image.width, "height": result.image.height},
            source,
        )
        return {
            "path": str(target_path),
            "format": image_format,
            "width": result.image.width,
            "height": result.image.height,
            "stats": result.stats.to_dict(),
            "adjustments": dict(document.params),
        }

    # ------------------------------------------------------------------
    # recipe.*
    # ------------------------------------------------------------------
    def _cmd_recipe_save(self, params: dict, source: str) -> dict:
        document = self._require_document()
        path = document.save_sidecar()
        self.events.emit("recipe.saved", {"path": str(path)}, source)
        return {"path": str(path), "recipe": document.recipe()}

    def _cmd_recipe_reload(self, params: dict, source: str) -> dict:
        document = self._require_document()
        if not document.sidecar_path.exists():
            raise not_found(f"没有找到 sidecar：{document.sidecar_path}",
                            "先执行 recipe.save 生成一个。")
        document.load_sidecar()
        self._preview_cache.clear()
        self.history.push(document.params, label="载入 sidecar", source=source)
        return {"adjustments": dict(document.params)}

    # ------------------------------------------------------------------
    # events.*
    # ------------------------------------------------------------------
    def _cmd_events_list(self, params: dict, source: str) -> dict:
        since = int(params.get("since") or 0)
        limit = int(params.get("limit") or 200)
        items = self.events.list_since(since, limit)
        return {
            "since": since,
            "last_seq": self.events.last_seq,
            "count": len(items),
            "events": [e.to_dict() for e in items],
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def preview_png(self, max_side: int | None = None) -> bytes:
        """给 HTTP 层用的便捷方法：直接拿一张 PNG 预览的字节流。"""
        result = self._render_preview(max_side or self.settings.preview_max_side)
        buffer = io.BytesIO()
        result.image.save(buffer, format="PNG")
        return buffer.getvalue()

    def source_image_size(self) -> tuple[int, int] | None:
        if self.document is None:
            return None
        return self.document.width, self.document.height

    def thumbnail_bgr(self) -> Image.Image | None:  # pragma: no cover - 预留
        return self.document.preview_source if self.document else None
