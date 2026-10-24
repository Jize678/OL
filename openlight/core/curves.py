"""曲线数学：控制点 -> 查找表（LUT）。

两种曲线共用一套表示法：
- 点曲线（用户拖点）：Fritsch-Carlson 单调三次插值，不会过冲、不会反相，
  和 Lightroom 的做法一致；
- 色调曲线（四个分区滑块）：升余弦权重构成单位分解，叠加成平滑曲线。

结果都存成 0~1 的等距查找表。渲染时用「量化 + 花式索引」查表，
比每个像素做插值快一个数量级。
"""

from __future__ import annotations

import numpy as np

LUT_SIZE = 4096
HSL_FIELD_SIZE = 1024

# 两个控制点挨得太近会让插值数值不稳定
MIN_POINT_GAP = 1e-4

TONE_REGIONS = (
    ("tone_shadows", 0.0),
    ("tone_darks", 1.0 / 3.0),
    ("tone_lights", 2.0 / 3.0),
    ("tone_highlights", 1.0),
)
TONE_HALF_WIDTH = 1.0 / 3.0
TONE_STRENGTH = 0.35      # 单个分区滑到 ±100 时的最大位移


def identity_points() -> list[list[float]]:
    """默认曲线：45 度直线，不做任何改变。"""
    return [[0.0, 0.0], [1.0, 1.0]]


def normalize_points(points, *, minimum: int = 2) -> list[list[float]]:
    """把 agent / 界面传来的控制点整理成规范形式。

    规则：坐标夹到 0~1、按 x 升序、同 x 只保留一个、至少 minimum 个点。
    """
    if points is None or not isinstance(points, (list, tuple)):
        return identity_points()

    cleaned: list[list[float]] = []
    for item in points:
        if isinstance(item, dict):
            x, y = item.get("x"), item.get("y")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = item[0], item[1]
        else:
            continue
        try:
            x = float(x)
            y = float(y)
        except (TypeError, ValueError):
            continue
        cleaned.append([min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)])

    cleaned.sort(key=lambda point: point[0])
    deduped: list[list[float]] = []
    for point in cleaned:
        if deduped and point[0] - deduped[-1][0] < MIN_POINT_GAP:
            continue
        deduped.append(point)

    if not deduped:
        return identity_points()
    if len(deduped) == 1:
        level = deduped[0][1]
        return [[0.0, level], [1.0, level]]
    while len(deduped) < minimum:
        deduped.insert(0, [0.0, 0.0])
    return deduped


def is_identity(points) -> bool:
    """是否就是默认直线（是的话渲染时整段跳过）。"""
    pts = normalize_points(points)
    if len(pts) != 2:
        return False
    return (abs(pts[0][0]) < 1e-6 and abs(pts[0][1]) < 1e-6
            and abs(pts[1][0] - 1.0) < 1e-6 and abs(pts[1][1] - 1.0) < 1e-6)


def monotone_lut(points, size: int = LUT_SIZE) -> np.ndarray:
    """单调三次插值 -> LUT。"""
    pts = normalize_points(points)
    xs = np.array([point[0] for point in pts], dtype=np.float64)
    ys = np.array([point[1] for point in pts], dtype=np.float64)
    count = len(xs)

    if count == 2 and is_identity(pts):
        return np.linspace(0.0, 1.0, size, dtype=np.float32)

    slopes = np.diff(ys) / np.maximum(np.diff(xs), 1e-12)

    tangents = np.empty(count, dtype=np.float64)
    tangents[0] = slopes[0]
    tangents[-1] = slopes[-1]
    if count > 2:
        tangents[1:-1] = (slopes[:-1] + slopes[1:]) * 0.5

    # Fritsch-Carlson 修正：切线不超过割线斜率的三倍，杜绝过冲
    for index in range(count - 1):
        if slopes[index] == 0.0:
            tangents[index] = 0.0
            tangents[index + 1] = 0.0
            continue
        alpha = tangents[index] / slopes[index]
        beta = tangents[index + 1] / slopes[index]
        magnitude = alpha * alpha + beta * beta
        if magnitude > 9.0:
            scale = 3.0 / np.sqrt(magnitude)
            tangents[index] = scale * alpha * slopes[index]
            tangents[index + 1] = scale * beta * slopes[index]

    grid = np.linspace(0.0, 1.0, size, dtype=np.float64)
    index = np.clip(np.searchsorted(xs, grid, side="right") - 1, 0, count - 2)
    x0, x1 = xs[index], xs[index + 1]
    y0, y1 = ys[index], ys[index + 1]
    span = np.maximum(x1 - x0, 1e-12)
    t = np.clip((grid - x0) / span, 0.0, 1.0)

    t2 = t * t
    t3 = t2 * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    values = (h00 * y0 + h10 * span * tangents[index]
              + h01 * y1 + h11 * span * tangents[index + 1])

    # 数值安全网：保证单调不减，防止出现反相断层
    values = np.maximum.accumulate(np.clip(values, 0.0, 1.0))
    return values.astype(np.float32)


def _raised_cosine(x: np.ndarray, center: float, half_width: float) -> np.ndarray:
    offset = np.abs(x - center) / half_width
    return np.where(offset >= 1.0, 0.0,
                    0.5 * (1.0 + np.cos(np.pi * np.clip(offset, 0.0, 1.0))))


def parametric_lut(values: dict, size: int = LUT_SIZE,
                   strength: float = TONE_STRENGTH) -> np.ndarray:
    """色调曲线的四个分区 -> LUT。values 的键是 TONE_REGIONS 里的 id。"""
    grid = np.linspace(0.0, 1.0, size, dtype=np.float64)
    delta = np.zeros_like(grid)
    for region_id, center in TONE_REGIONS:
        amount = float(values.get(region_id, 0.0) or 0.0) / 100.0
        if abs(amount) < 1e-9:
            continue
        weight = _raised_cosine(grid, center, TONE_HALF_WIDTH)
        headroom = np.where(amount > 0, 1.0 - grid, grid)
        delta += amount * strength * weight * headroom
    result = np.clip(grid + delta, 0.0, 1.0)
    return np.maximum.accumulate(result).astype(np.float32)


def sample_lut(lut: np.ndarray, values: np.ndarray) -> np.ndarray:
    """按值查表（values 为 0~1）。"""
    size = len(lut)
    index = np.clip((values * (size - 1)).astype(np.int32), 0, size - 1)
    return lut[index]


def compose_lut(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """先走 first，再走 second。"""
    return sample_lut(second.astype(np.float32), first.astype(np.float32))


def hsl_field_lut(band_values: dict, attribute: str,
                  size: int = HSL_FIELD_SIZE) -> np.ndarray:
    """8 个色带的取值按色相插值成一张细查表。

    分段线性插值 + 首尾环绕，所以调橙色不会在红/黄交界处出现硬边。
    """
    from .color import BAND_CENTERS, BAND_ORDER

    centers = [BAND_CENTERS[band] for band in BAND_ORDER]
    values = [float(band_values.get("hsl_%s_%s" % (band, attribute), 0.0) or 0.0)
              for band in BAND_ORDER]

    xs = np.array([centers[-1] - 1.0, *centers, centers[0] + 1.0], dtype=np.float64)
    ys = np.array([values[-1], *values, values[0]], dtype=np.float64)
    hue = np.linspace(0.0, 1.0, size, dtype=np.float64)
    return np.interp(hue, xs, ys).astype(np.float32)