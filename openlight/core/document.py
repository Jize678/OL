"""照片文档：源图 + 调整参数，非破坏性。

原图文件永远不被改写。所有调整只存在于内存里的 params 字典，
可以随时写进 sidecar（`原文件名.openlight.json`）保存下来。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from . import engine
from .adjustments import registry
from .errors import OpenLightError

# 界面预览用的源图长边上限。真正的导出仍然用原始分辨率。
PREVIEW_SOURCE_MAX_SIDE = 2048

SUPPORTED_SUFFIXES = {
    ".jpg", ".jpeg", ".jpe", ".png", ".bmp", ".tif", ".tiff", ".webp", ".gif",
}


def _file_digest(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _exif_summary(image: Image.Image) -> dict:
    """抽取一点拍摄参数。agent 靠这些信息判断「这照片该怎么修」。"""
    try:
        exif = image.getexif()
    except Exception:
        return {}
    if not exif:
        return {}

    tags = {
        271: "make",          # 厂商
        272: "model",         # 机型
        305: "software",
        34855: "iso",
        33434: "exposure_time",
        33437: "f_number",
        37386: "focal_length",
        36867: "datetime_original",
    }
    summary: dict = {}
    for tag, name in tags.items():
        value = exif.get(tag)
        if value is None:
            continue
        if name == "exposure_time":
            try:
                value = float(value)
                value = f"1/{round(1 / value)} s" if value and value < 1 else f"{value:g} s"
            except Exception:
                value = str(value)
        elif name in ("f_number", "focal_length"):
            try:
                value = float(value)
            except Exception:
                value = str(value)
        elif isinstance(value, (int, float)):
            pass
        else:
            value = str(value).strip()
        summary[name] = value
    return summary


@dataclass
class PhotoDocument:
    """一张被打开的照片。"""

    path: Path
    image: Image.Image                       # 原始分辨率，RGB
    preview_source: Image.Image              # 长边 <= PREVIEW_SOURCE_MAX_SIDE
    params: dict[str, Any] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self) -> None:
        base = registry.default_params()
        params = self.params or {}
        for key, value in params.items():
            if not registry.has(key):
                continue
            try:
                base[key] = registry.coerce(key, value)
            except (TypeError, ValueError):
                continue
        self.params = base

    # -- 属性 ---------------------------------------------------------------
    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def sidecar_path(self) -> Path:
        return self.path.with_name(self.path.name + ".openlight.json")

    @property
    def is_modified(self) -> bool:
        return any(
            not registry.is_default(key, self.params.get(key))
            for key in registry.ids()
        )

    # -- 参数 ---------------------------------------------------------------
    def set_param(self, adjustment_id: str, value: Any) -> None:
        if not registry.has(adjustment_id):
            raise OpenLightError(
                "unknown_adjustment",
                f"没有名为 {adjustment_id} 的调整项。",
                "调用 app.schema，在 adjustments 字段里查看全部可用调整项。",
            )
        try:
            self.params[adjustment_id] = registry.coerce(adjustment_id, value)
        except (TypeError, ValueError) as exc:
            raise OpenLightError(
                "invalid_value",
                "调整项 %s 的取值不合法：%s" % (adjustment_id, exc),
                "标量参数传数字；曲线参数传 [[x, y], ...] 且 x 必须递增。",
            ) from exc

    def reset_param(self, adjustment_id: str) -> None:
        self.set_param(adjustment_id, registry.get(adjustment_id).default_value())

    def reset_all(self) -> None:
        self.params = registry.default_params()

    def active_params(self) -> dict:
        """只返回偏离默认值的参数，便于人和 agent 一眼看出改了什么。"""
        return {
            key: value
            for key, value in self.params.items()
            if not registry.is_default(key, value)
        }

    # -- 渲染 ---------------------------------------------------------------
    def render_preview(self, max_side: int = 1600) -> engine.RenderResult:
        return engine.engine.render(self.preview_source, self.params, max_side=max_side)

    def render_full(self) -> engine.RenderResult:
        return engine.engine.render(self.image, self.params)

    # -- sidecar ------------------------------------------------------------
    def recipe(self) -> dict:
        return {
            "format": "openlight.recipe/1",
            "app": "openLight",
            "source": str(self.path),
            "source_digest": self.meta.get("digest"),
            "updated_at": time.time(),
            "adjustments": dict(self.params),
        }

    def save_sidecar(self) -> Path:
        path = self.sidecar_path
        path.write_text(
            json.dumps(self.recipe(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def load_sidecar(self) -> bool:
        path = self.sidecar_path
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        adjustments = data.get("adjustments") or {}
        for key, value in adjustments.items():
            if not registry.has(key):
                continue
            try:
                self.params[key] = registry.coerce(key, value)
            except (TypeError, ValueError):
                continue
        return True

    # -- 描述 ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": str(self.path),
            "filename": self.filename,
            "width": self.width,
            "height": self.height,
            "megapixels": round(self.width * self.height / 1e6, 2),
            "modified": self.is_modified,
            "adjustments": dict(self.params),
            "active_adjustments": self.active_params(),
            "meta": self.meta,
            "sidecar": str(self.sidecar_path) if self.sidecar_path.exists() else None,
        }


def open_document(path: str | Path, *, params: dict | None = None,
                  load_sidecar: bool = True) -> PhotoDocument:
    """从磁盘打开一张照片。"""
    target = Path(path).expanduser()
    if not target.exists():
        raise OpenLightError(
            "file_not_found",
            f"找不到文件：{target}",
            "确认路径是否正确，注意 Windows 路径里的反斜杠在 JSON 里要写成 \\\\。",
        )
    if not target.is_file():
        raise OpenLightError("not_a_file", f"这不是文件：{target}")
    if target.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise OpenLightError(
            "unsupported_format",
            f"暂不支持 {target.suffix} 格式。",
            "支持：" + "、".join(sorted(SUPPORTED_SUFFIXES)),
        )

    try:
        with Image.open(target) as handle:
            handle.load()
            image = ImageOps.exif_transpose(handle).convert("RGB")
            exif = _exif_summary(handle)
            image_format = handle.format
    except OpenLightError:
        raise
    except Exception as exc:
        raise OpenLightError(
            "decode_failed", f"图片解码失败：{exc}", "换一张图片试试，或先转成 JPEG/PNG。"
        ) from exc

    preview_source = image.copy()
    preview_source.thumbnail(
        (PREVIEW_SOURCE_MAX_SIDE, PREVIEW_SOURCE_MAX_SIDE), Image.LANCZOS
    )

    stat = target.stat()
    meta = {
        "format": image_format,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "digest": _file_digest(target),
        "exif": exif,
    }

    document = PhotoDocument(
        path=target, image=image, preview_source=preview_source, params=params or {}, meta=meta
    )
    # sidecar 的优先级低于显式传入的 params。
    if load_sidecar and not params:
        document.load_sidecar()
    return document
