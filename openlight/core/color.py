"""颜色空间工具：sRGB 线性化、HSL 互转、HSL 混色器的色带定义。

所有函数都是纯函数、numpy 向量化，输入输出都用 0~1 的 float32。

性能约定（预览要跟手，这些细节很关键）：
- sRGB 编解码走查找表，不用 np.power 逐像素算；
- HSL 互转按通道直接选值，不拼接 (3, H, W) 的大数组，也避开 take_along_axis
  这类慢速聚合（实测比最初的写法快一倍以上）。
"""

from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# sRGB <-> 线性光
# ---------------------------------------------------------------------------
def _build_srgb_to_linear_lut() -> np.ndarray:
    x = np.arange(256, dtype=np.float32) / np.float32(255.0)
    lut = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    return lut.astype(np.float32)


SRGB_TO_LINEAR: np.ndarray = _build_srgb_to_linear_lut()

# 线性 -> sRGB 的编码表。16384 档在线性空间的步长 6.1e-5，
# 最暗处折算到输出也只有 0.0008（远小于 1/255），不会出现断层。
ENCODE_LUT_SIZE = 16384


def _build_linear_to_srgb_lut() -> np.ndarray:
    x = np.linspace(0.0, 1.0, ENCODE_LUT_SIZE, dtype=np.float64)
    lut = np.where(x <= 0.0031308, x * 12.92,
                   1.055 * np.power(x, 1.0 / 2.4) - 0.055)
    return np.clip(lut, 0.0, 1.0).astype(np.float32)


LINEAR_TO_SRGB: np.ndarray = _build_linear_to_srgb_lut()


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    """线性光 -> sRGB 编码（查表，输入可以超过 1，会被夹住）。"""
    index = np.clip((linear * np.float32(ENCODE_LUT_SIZE - 1)).astype(np.int32),
                    0, ENCODE_LUT_SIZE - 1)
    return LINEAR_TO_SRGB[index]


# ---------------------------------------------------------------------------
# HSL 互转（HSL 混色器要用；h 用 0~1 表示一周）
# ---------------------------------------------------------------------------
def rgb_to_hsl(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(..., 3) 的 RGB -> (h, s, l)，h 用 0~1 表示一周。"""
    red = rgb[..., 0]
    green = rgb[..., 1]
    blue = rgb[..., 2]

    # 用 maximum/minimum 链代替 np.max(stack, axis=0)：少一次 (3,H,W) 的拷贝，
    # 也避开了 argmax + take_along_axis 这种慢速聚合。
    maximum = np.maximum(np.maximum(red, green), blue)
    minimum = np.minimum(np.minimum(red, green), blue)

    lightness = (maximum + minimum) * np.float32(0.5)
    delta = maximum - minimum

    # |2L-1| == |max+min-1|，省一次乘加
    denominator = np.maximum(
        np.float32(1.0) - np.abs(maximum + minimum - np.float32(1.0)),
        np.float32(1e-6))
    saturation = delta / denominator

    safe = np.where(delta < np.float32(1e-9), np.float32(1.0), delta)
    red_c = (maximum - red) / safe
    green_c = (maximum - green) / safe
    blue_c = (maximum - blue) / safe

    hue = np.where(
        maximum == red, blue_c - green_c,
        np.where(maximum == green, np.float32(2.0) + red_c - blue_c,
                 np.float32(4.0) + green_c - red_c))
    hue *= np.float32(1.0 / 6.0)
    hue %= np.float32(1.0)

    np.clip(saturation, 0.0, 1.0, out=saturation)
    return hue, saturation, lightness


# 六个扇区里 (R, G, B) 各自取 c / x / 0：
#   _RC 表示「这个通道取 chroma」，_RX 表示「这个通道取 second」
_RC = np.array([1, 0, 0, 0, 0, 1], dtype=bool)
_RX = np.array([0.0, 1.0, 0.0, 0.0, 1.0, 0.0], dtype=np.float32)
_GC = np.array([0, 1, 1, 0, 0, 0], dtype=bool)
_GX = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0], dtype=np.float32)
_BC = np.array([0, 0, 0, 1, 1, 0], dtype=bool)
_BX = np.array([0.0, 0.0, 1.0, 0.0, 0.0, 1.0], dtype=np.float32)


def hsl_to_rgb(hue: np.ndarray, saturation: np.ndarray,
               lightness: np.ndarray) -> np.ndarray:
    """(h, s, l) -> (..., 3) 的 RGB。"""
    if not (hue.shape == saturation.shape == lightness.shape):
        raise ValueError("hue/saturation/lightness 形状必须一致")

    hue = np.mod(hue, np.float32(1.0))
    # |2L-1| 先算，省一次 (H,W) 的中间量
    chroma = np.float32(1.0) - np.abs(lightness * np.float32(2.0) - np.float32(1.0))
    np.clip(chroma, 0.0, 1.0, out=chroma)
    chroma *= saturation

    hon = hue * np.float32(6.0)
    second = np.mod(hon, np.float32(2.0))
    second -= np.float32(1.0)
    np.abs(second, out=second)
    second -= np.float32(1.0)
    np.abs(second, out=second)
    second *= chroma

    sector = np.clip(hon, 0, np.float32(5.999999)).astype(np.intp)

    # 直接按通道选 c / x / 0，省掉 (3,H,W) 的拼接和 take_along_axis
    output = np.empty(hue.shape + (3,), dtype=np.float32)
    output[..., 0] = np.where(_RC[sector], chroma, second * _RX[sector])
    output[..., 1] = np.where(_GC[sector], chroma, second * _GX[sector])
    output[..., 2] = np.where(_BC[sector], chroma, second * _BX[sector])
    output += (lightness - chroma * np.float32(0.5))[..., None]
    return np.clip(output, 0.0, 1.0, out=output)


# ---------------------------------------------------------------------------
# HSL 混色器的 8 个色带
# ---------------------------------------------------------------------------
BAND_ORDER = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")

BAND_LABELS = {
    "red": "红",
    "orange": "橙",
    "yellow": "黄",
    "green": "绿",
    "aqua": "青",
    "blue": "蓝",
    "purple": "紫",
    "magenta": "洋红",
}

# 色带中心在色环上的位置（0~1，0 表示纯红）
BAND_CENTERS = {
    "red": 0.0 / 360.0,
    "orange": 30.0 / 360.0,
    "yellow": 60.0 / 360.0,
    "green": 120.0 / 360.0,
    "aqua": 180.0 / 360.0,
    "blue": 240.0 / 360.0,
    "purple": 290.0 / 360.0,
    "magenta": 320.0 / 360.0,
}

# 界面上用的色块颜色
BAND_COLORS = {
    "red": "#d95b5b",
    "orange": "#d98b3c",
    "yellow": "#c9b83f",
    "green": "#57a85c",
    "aqua": "#48adb8",
    "blue": "#4d7fe0",
    "purple": "#8f5fd0",
    "magenta": "#cf59a8",
}

BAND_ATTRIBUTES = ("hue", "sat", "lum")

BAND_ATTRIBUTE_LABELS = {"hue": "色相", "sat": "饱和度", "lum": "明亮度"}