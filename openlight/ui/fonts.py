"""字体兜底：保证界面上的中文真的能画出字形，不会变成豆腐块（□□□）。

为什么需要这一层：
Qt 的 ``offscreen`` 平台插件（截图、离屏测试、没有桌面的环境用它）不接 Windows 的
GDI 字库，而是走 FreeType 自己的字库目录；而 Qt 6 已经不再随包附带字体。于是
``QFontDatabase.families()`` 可能返回空列表 —— 此时每个汉字都会被画成方框。
正常桌面（windows 平台插件）不会遇到，但``tools/screenshot.py``、
``tools/uitest.py`` 恰好都在 offscreen 下跑，所以截图会整片变成豆腐块。

策略是「先找、再注册、最后明确失败」：

1. 系统字库里已经有能覆盖中文的字族 —— 直接用（正常桌面走这条路，零成本）；
2. 字库是空的 —— 把系统字体文件（msyh.ttc / Deng.ttf / NotoSansCJK / PingFang…）
   用 :func:`QFontDatabase.addApplicationFont` 注册进 Qt，再取它暴露的字族；
3. 都失败 —— 抛 :class:`FontUnavailable`。宁可报错，也不产出满是豆腐块的截图。

另外提供 :func:`missing_glyphs` / :func:`audit_widgets`：把「有没有缺字形」变成
可断言的检查，让测试和截图工具都能在出图前确认「这一张必定没有豆腐块」。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtGui import QAction, QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QWidget

# 用来判断「这个字族能不能画中文」的样本：覆盖界面里最常见的几类字，
# 又多选几个不同部首，避免某个字族只覆盖了 GB2312 的一部分。
CJK_SAMPLE = "色温色调曝光曲线混色器已调平均亮度高光削波暗部恢复"

# 优先使用的字族：名字就是 Windows / macOS / Linux 上最常见的几个中文字体。
PREFERRED_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Hiragino Sans GB",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "Source Han Sans CN",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "DengXian",
    "SimHei",
    "SimSun",
)

# 字库为空时按这个顺序注册字体文件。先雅黑（字形最接近界面设计），
# 再等线 / 黑体 / 宋体兜底，最后是跨平台的 Noto。
_WINDOWS_FONT_FILES = (
    "msyh.ttc", "msyhbd.ttc", "msjhl.ttc", "msjh.ttc",
    "Deng.ttf", "simhei.ttf", "simsun.ttc", "simkai.ttf",
    "NotoSansSC-VF.ttf",
)

_OTHER_FONT_FILES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


class FontUnavailable(RuntimeError):
    """系统里没有任何能覆盖中文的字体，界面无法正确渲染。"""


# 缓存：字族 -> 是否能覆盖样本；单字 -> 是否有字形。QFontMetrics 创建不贵，
# 但审计一个窗口会问上千次，缓存下来省事。
_FAMILY_CACHE: dict[str, bool] = {}
_CHAR_CACHE: dict[tuple[str, str], bool] = {}
_RESOLVED: str | None = None


# ---------------------------------------------------------------------------
# 找字体
# ---------------------------------------------------------------------------
def font_files() -> list[Path]:
    """候选字体文件（存在的才返回），环境变量 OPENLIGHT_FONT 排在最前。"""
    candidates: list[Path] = []

    override = os.environ.get("OPENLIGHT_FONT")
    if override:
        candidates.append(Path(override).expanduser())

    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates.extend(windows_dir / name for name in _WINDOWS_FONT_FILES)
    candidates.extend(Path(path) for path in _OTHER_FONT_FILES)

    # 也看看用户自己装的字体目录
    local_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    if local_fonts.is_dir():
        candidates.extend(local_fonts / name for name in _WINDOWS_FONT_FILES)

    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        result.append(path)
    return result


def covers(family: str, sample: str = CJK_SAMPLE) -> bool:
    """这个字族能不能把样本里的字全部画出来。"""
    if not family:
        return False
    key = f"{family}\x00{sample}"
    cached = _FAMILY_CACHE.get(key)
    if cached is not None:
        return cached
    metrics = QFontMetrics(QFont(family))
    ok = True
    for char in dict.fromkeys(sample):
        if char.isspace():
            continue
        if not metrics.inFontUcs4(ord(char)):
            ok = False
            break
    _FAMILY_CACHE[key] = ok
    return ok


def _covers_char(family: str, char: str) -> bool:
    key = (family, char)
    cached = _CHAR_CACHE.get(key)
    if cached is not None:
        return cached
    ok = QFontMetrics(QFont(family)).inFontUcs4(ord(char))
    _CHAR_CACHE[key] = ok
    return ok


def _register(path: Path) -> list[str]:
    """把字体文件注册进 Qt，返回它暴露的字族列表。"""
    font_id = QFontDatabase.addApplicationFont(str(path))
    if font_id < 0:
        return []
    return list(QFontDatabase.applicationFontFamilies(font_id))


def ensure_cjk_font(sample: str = CJK_SAMPLE) -> str:
    """返回一个「确定能画出中文」的字族名；找不到就抛 :class:`FontUnavailable`。

    可以在 QApplication 创建之后调用一次，然后把返回值同时交给
    ``QApplication.setFont`` 和 ``theme.stylesheet(family)``。
    """
    global _RESOLVED
    if _RESOLVED and sample == CJK_SAMPLE:
        return _RESOLVED

    families = QFontDatabase.families()
    available = set(families)

    # 1) 系统字库里已经有能用的字族
    for name in PREFERRED_FAMILIES:
        if name in available and covers(name, sample):
            if sample == CJK_SAMPLE:
                _RESOLVED = name
            return name
    for name in families:
        if covers(name, sample):
            if sample == CJK_SAMPLE:
                _RESOLVED = name
            return name

    # 2) 字库是空的（offscreen 插件）——把系统字体文件注册进来
    tried: list[str] = []
    for path in font_files():
        tried.append(str(path))
        for name in _register(path):
            if covers(name, sample):
                if sample == CJK_SAMPLE:
                    _RESOLVED = name
                return name

    hint = ("没有找到能覆盖中文的字体。\n"
            "  · Windows：确认 C:\\Windows\\Fonts 里有 msyh.ttc / simhei.ttf / simsun.ttc；\n"
            "  · Linux：安装 fonts-noto-cjk 或 fonts-wqy-microhei；\n"
            "  · 或者用环境变量 OPENLIGHT_FONT 指定一个 .ttf/.ttc/.otf 字体文件。")
    details = "\n  已尝试：" + ", ".join(tried) if tried else ""
    raise FontUnavailable(f"界面需要中文字体，但系统里一个都没找到。\n{hint}{details}")


def default_family() -> str:
    """已经解析出来的字族（没解析过就返回空串）。"""
    return _RESOLVED or ""


# ---------------------------------------------------------------------------
# 检查「有没有缺字形」——把豆腐块变成可以断言的布尔值
# ---------------------------------------------------------------------------
def missing_glyphs(text: str, families: Sequence[str] | str | None = None) -> str:
    """返回 text 里没有任何一个给定字族能画出来的字符（按出现顺序去重）。

    只检查非 ASCII 字符：ASCII 一定有字体覆盖，不值得报。
    """
    if isinstance(families, str):
        families = [families]
    if families is None:
        resolved = default_family()
        families = [resolved] if resolved else list(QFontDatabase.families())
    families = [name for name in families if name]
    if not families:
        return "".join(sorted({ch for ch in text if ord(ch) > 127}))

    missing: list[str] = []
    for char in dict.fromkeys(text):
        if char.isspace() or ord(char) < 128:
            continue
        if not any(_covers_char(name, char) for name in families):
            missing.append(char)
    return "".join(missing)


def audit_widgets(root: QWidget) -> list[str]:
    """扫描一个窗口里的所有文字，返回「会变成豆腐块」的地方。

    每一条的形状是 ``类型(对象名): 缺字形 'x' ← "出现这段文字的控件内容"``。
    空列表表示这一屏的字都有字形，画出来一定不是豆腐块。
    """
    problems: list[str] = []
    for widget in root.findChildren(QWidget):
        families = list(widget.font().families())
        if not families:
            resolved = default_family()
            families = [resolved] if resolved else []
        texts: list[str] = []
        for getter in ("text", "title", "windowTitle", "placeholderText"):
            try:
                value = getattr(widget, getter)()
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                texts.append(value)
        missing = [missing_glyphs(text, families) for text in texts]
        for text, chars in zip(texts, missing):
            if chars:
                label = widget.objectName() or type(widget).__name__
                problems.append(
                    f"{type(widget).__name__}({label}): 缺字形 {chars!r} ← {text.strip()[:40]!r}"
                )

    for action in root.findChildren(QAction):
        text = action.text()
        if not text:
            continue
        families = [default_family()] if default_family() else []
        chars = missing_glyphs(text, families)
        if chars:
            problems.append(f"QAction({action.objectName() or '-'}): 缺字形 {chars!r} ← {text!r}")

    title = root.windowTitle()
    if title:
        families = [default_family()] if default_family() else []
        chars = missing_glyphs(title, families)
        if chars:
            problems.append(f"窗口标题: 缺字形 {chars!r} ← {title!r}")
    return problems


def describe() -> dict:
    """给日志/诊断用的字体现状。"""
    families = QFontDatabase.families()
    return {
        "resolved": default_family(),
        "family_count": len(families),
        "files": [str(path) for path in font_files()],
        "has_cjk_family": any(covers(name) for name in families[:200]),
    }


__all__ = ["CJK_SAMPLE", "FontUnavailable", "audit_widgets", "covers",
           "default_family", "describe", "ensure_cjk_font", "font_files",
           "missing_glyphs"]
