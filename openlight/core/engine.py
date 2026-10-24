"""渲染引擎：把「源图 + 参数」编译成一条处理管线。

设计要点
--------
1. **纯函数**：同样的输入永远得到同样的输出，没有隐藏状态，agent 可以放心重放。
2. **编译一次，跑很多次**：曲线、色带插值、增益这些都在 RenderPlan 里预先算好，
   每个像素只做查表和算术，不做重复的 pow / 插值。
3. **分块处理**：大图按行分块，峰值内存可控，24MP 也不会炸。
4. **能跳过就跳过**：默认值的那一级整段不执行；全部默认时直接返回原图，
   保证「什么都没调」＝「像素一模一样」。
5. **返回统计**：告诉调用者亮度均值和高光/暗部削波比例，agent 靠它判断调没调过头。

处理顺序（和 Lightroom 的直觉一致）：
    线性空间：白平衡 -> 曝光
    显示空间：白色/黑色色阶 -> 对比度 -> 高光/阴影 -> 色调曲线
              -> 点曲线(RGB -> 单通道) -> HSL 混色器 -> 自然饱和度 -> 饱和度
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .adjustments import registry
from .color import SRGB_TO_LINEAR, hsl_to_rgb, linear_to_srgb, rgb_to_hsl
from .curves import (TONE_REGIONS, compose_lut, hsl_field_lut, is_identity,
                     monotone_lut, parametric_lut, sample_lut)

# 一次处理多少行，兼顾速度和内存。4000 行 x 6000 列 x 3 通道 x 4 字节 ≈ 275MB，
# 取 512 行把峰值控制在 ~35MB 量级。
_STRIP_ROWS = 512

# 各级的强度系数。这些数字决定了「滑块到底有多猛」，是调过手感的经验值。
_WB_TEMP_GAIN = 0.30
_WB_TINT_GAIN = 0.08
_WB_TINT_GREEN = 0.16
_LEVELS_RANGE = 0.15
_CONTRAST_GAIN = 1.2
_HIGHLIGHT_GAIN = 0.75
_SHADOW_GAIN = 0.90
_HSL_HUE_DEGREES = 30.0
_HSL_SAT_GAIN = 0.85
_HSL_LUM_SHIFT = 0.30

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _number(params: dict, key: str, default: float = 0.0) -> float:
    try:
        value = params.get(key, default)
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RenderStats:
    """渲染结果的健康度指标，供 agent 判断调整是否过头。"""

    mean_luma: float = 0.0
    clipped_highlights_pct: float = 0.0   # 被推到纯白的像素比例
    clipped_shadows_pct: float = 0.0      # 被压到纯黑的像素比例
    gain: float = 1.0                     # 曝光实际使用的线性增益
    identity: bool = False                # 是否原样返回（所有参数都是默认值）
    applied: list = field(default_factory=list)   # 实际生效的处理级

    def to_dict(self) -> dict:
        return {
            "mean_luma": round(self.mean_luma, 4),
            "clipped_highlights_pct": round(self.clipped_highlights_pct, 3),
            "clipped_shadows_pct": round(self.clipped_shadows_pct, 3),
            "gain": round(self.gain, 6),
            "identity": self.identity,
            "applied": list(self.applied),
        }


@dataclass
class RenderResult:
    image: Image.Image
    stats: RenderStats = field(default_factory=RenderStats)


class RenderPlan:
    """把参数字典编译成可执行的管线。"""

    def __init__(self, params: dict | None = None) -> None:
        params = dict(params or {})
        self.temperature = _number(params, "temperature")
        self.tint = _number(params, "tint")
        self.exposure = _number(params, "exposure")
        self.contrast = _number(params, "contrast")
        self.highlights = _number(params, "highlights")
        self.shadows = _number(params, "shadows")
        self.whites = _number(params, "whites")
        self.blacks = _number(params, "blacks")
        self.vibrance = _number(params, "vibrance")
        self.saturation = _number(params, "saturation")

        # --- 线性空间：白平衡 + 曝光 ---
        warm = self.temperature / 100.0
        magenta = self.tint / 100.0
        exposure_gain = 2.0 ** self.exposure
        self.gain = exposure_gain
        self.gain_r = (1.0 + _WB_TEMP_GAIN * warm + _WB_TINT_GAIN * magenta) * exposure_gain
        self.gain_g = (1.0 - _WB_TINT_GREEN * magenta) * exposure_gain
        self.gain_b = (1.0 - _WB_TEMP_GAIN * warm + _WB_TINT_GAIN * magenta) * exposure_gain
        self.white_balance = abs(self.temperature) > 1e-9 or abs(self.tint) > 1e-9
        self.exposure_active = abs(self.exposure) > 1e-9
        self.linear_active = self.white_balance or self.exposure_active

        # --- 显示空间：白色/黑色色阶 ---
        self.black_point = -self.blacks / 100.0 * _LEVELS_RANGE
        self.white_point = 1.0 - self.whites / 100.0 * _LEVELS_RANGE
        if self.white_point - self.black_point < 0.25:
            self.white_point = self.black_point + 0.25
        self.levels_active = abs(self.blacks) > 1e-9 or abs(self.whites) > 1e-9

        self.contrast_active = abs(self.contrast) > 1e-9
        self.contrast_amount = self.contrast / 100.0 * _CONTRAST_GAIN

        self.highlight_active = abs(self.highlights) > 1e-9
        self.shadow_active = abs(self.shadows) > 1e-9
        self.region_active = self.highlight_active or self.shadow_active

        # --- 色调曲线（分区滑块）---
        tone_values = {key: _number(params, key) for key, _ in TONE_REGIONS}
        self.tone_active = any(abs(value) > 1e-9 for value in tone_values.values())
        self.tone_lut = parametric_lut(tone_values) if self.tone_active else None

        # --- 点曲线：RGB 曲线先走，单通道曲线后走 ---
        rgb_points = params.get("curve_rgb")
        self.curve_rgb_active = not is_identity(rgb_points)
        base_lut = monotone_lut(rgb_points) if self.curve_rgb_active else None
        self.channel_luts: list[np.ndarray | None] = [None, None, None]
        for index, key in enumerate(("curve_red", "curve_green", "curve_blue")):
            points = params.get(key)
            channel_lut = None if is_identity(points) else monotone_lut(points)
            if base_lut is not None and channel_lut is not None:
                self.channel_luts[index] = compose_lut(base_lut, channel_lut)
            elif base_lut is not None:
                self.channel_luts[index] = base_lut
            elif channel_lut is not None:
                self.channel_luts[index] = channel_lut
        self.curves_active = any(lut is not None for lut in self.channel_luts)

        # --- HSL 混色器 ---
        hsl_keys = [spec.id for spec in registry.by_group("HSL 混色器")]
        hsl_values = {key: _number(params, key) for key in hsl_keys}
        self.hsl_hue_active = any(abs(value) > 1e-9 for key, value in hsl_values.items()
                                  if key.endswith("_hue"))
        self.hsl_sat_active = any(abs(value) > 1e-9 for key, value in hsl_values.items()
                                  if key.endswith("_sat"))
        self.hsl_lum_active = any(abs(value) > 1e-9 for key, value in hsl_values.items()
                                  if key.endswith("_lum"))
        self.hsl_active = self.hsl_hue_active or self.hsl_sat_active or self.hsl_lum_active
        self.hsl_hue_lut = hsl_field_lut(hsl_values, "hue") if self.hsl_hue_active else None
        self.hsl_sat_lut = hsl_field_lut(hsl_values, "sat") if self.hsl_sat_active else None
        self.hsl_lum_lut = hsl_field_lut(hsl_values, "lum") if self.hsl_lum_active else None

        # --- 自然饱和度 / 饱和度 ---
        self.vibrance_active = abs(self.vibrance) > 1e-9
        self.saturation_active = abs(self.saturation) > 1e-9
        self.vibrance_amount = self.vibrance / 100.0
        self.saturation_amount = self.saturation / 100.0
        self.presence_active = self.vibrance_active or self.saturation_active

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return (self.linear_active or self.levels_active or self.contrast_active
                or self.region_active or self.tone_active or self.curves_active
                or self.hsl_active or self.presence_active)

    def applied(self) -> list[str]:
        names = []
        if self.white_balance:
            names.append("white_balance")
        if self.exposure_active:
            names.append("exposure")
        if self.levels_active:
            names.append("levels")
        if self.contrast_active:
            names.append("contrast")
        if self.region_active:
            names.append("highlights_shadows")
        if self.tone_active:
            names.append("tone_curve")
        if self.curves_active:
            names.append("point_curves")
        if self.hsl_active:
            names.append("hsl_mixer")
        if self.presence_active:
            names.append("vibrance_saturation")
        return names

    # ------------------------------------------------------------------
    @staticmethod
    def _hue_field(lut: np.ndarray, hue: np.ndarray) -> np.ndarray:
        index = np.clip((hue * (len(lut) - 1)).astype(np.int32), 0, len(lut) - 1)
        return lut[index]

    def process(self, strip: np.ndarray) -> np.ndarray:
        """处理一个行块（uint8 -> uint8）。"""
        # 1. 解码到线性光，做白平衡和曝光
        values = SRGB_TO_LINEAR[strip]
        if self.linear_active:
            values[..., 0] *= self.gain_r
            values[..., 1] *= self.gain_g
            values[..., 2] *= self.gain_b

        # 2. 回到显示空间，后面的调整都在这里做
        x = linear_to_srgb(values)
        del values

        # 3. 白点 / 黑点（levels 重映射）
        if self.levels_active:
            span = self.white_point - self.black_point
            x = np.clip((x - self.black_point) / span, 0.0, 1.0)

        # 4. 对比度：以 0.5 为支点的三次 S 形，端点固定
        if self.contrast_active:
            amount = self.contrast_amount
            x = np.clip(x + amount * x * (1.0 - x) * (2.0 * x - 1.0), 0.0, 1.0)

        # 5. 高光 / 阴影：按亮度的平滑权重做局部亮度增益
        if self.region_active:
            luma = x @ _LUMA
            if self.highlight_active:
                weight = (luma * luma)[..., None]
                x = x * (1.0 + self.highlights / 100.0 * _HIGHLIGHT_GAIN * weight)
            if self.shadow_active:
                weight = ((1.0 - luma) ** 2)[..., None]
                x = x * (1.0 + self.shadows / 100.0 * _SHADOW_GAIN * weight)
            x = np.clip(x, 0.0, 1.0)

        # 6. 色调曲线
        if self.tone_active and self.tone_lut is not None:
            x = sample_lut(self.tone_lut, x)

        # 7. 点曲线（RGB 与单通道已经合成到同一条 LUT 里）
        if self.curves_active:
            for index, lut in enumerate(self.channel_luts):
                if lut is not None:
                    x[..., index] = sample_lut(lut, x[..., index])

        # 8. HSL 混色器
        if self.hsl_active:
            hue, saturation, lightness = rgb_to_hsl(x)
            if self.hsl_hue_active and self.hsl_hue_lut is not None:
                hue = np.mod(hue + self._hue_field(self.hsl_hue_lut, hue)
                             * np.float32(_HSL_HUE_DEGREES / 360.0), 1.0)
            if self.hsl_sat_active and self.hsl_sat_lut is not None:
                field = self._hue_field(self.hsl_sat_lut, hue)
                saturation = np.clip(saturation * (1.0 + field * _HSL_SAT_GAIN), 0.0, 1.0)
            if self.hsl_lum_active and self.hsl_lum_lut is not None:
                field = self._hue_field(self.hsl_lum_lut, hue)
                # 乘饱和度当权重：接近灰的像素没有「色相」可言，
                # 不能被某个色带的滑块带走，这是 HSL 混色器和纯色相滤镜的关键差别。
                lightness = np.clip(
                    lightness + field * (_HSL_LUM_SHIFT * saturation), 0.0, 1.0)
            x = hsl_to_rgb(hue, saturation, lightness)

        # 9. 自然饱和度 -> 饱和度
        if self.presence_active:
            luma = (x @ _LUMA)[..., None]
            if self.vibrance_active:
                spread = x.max(axis=2) - x.min(axis=2)
                weight = ((1.0 - spread) ** 2)[..., None]
                x = luma + (x - luma) * (1.0 + self.vibrance_amount * weight)
            if self.saturation_active:
                factor = 1.0 + self.saturation_amount
                x = luma + (x - luma) * factor
            x = np.clip(x, 0.0, 1.0)

        return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


class RenderEngine:
    """渲染入口。新增调整项时改 RenderPlan，不要改这里的接口。"""

    def render(self, source: Image.Image, params: dict | None = None, *,
               max_side: int | None = None) -> RenderResult:
        """把源图按参数渲染成新图。max_side 不为空时先等比缩小（用于界面预览）。"""
        image = source if source.mode == "RGB" else source.convert("RGB")
        if max_side:
            image = image.copy()
            image.thumbnail((max_side, max_side), Image.LANCZOS)

        plan = RenderPlan(params)
        if not plan.active:
            return RenderResult(image=image.copy(),
                                stats=RenderStats(identity=True, applied=[]))

        array = np.asarray(image, dtype=np.uint8)
        height = array.shape[0]
        output = np.empty_like(array)

        highlight_hits = 0
        shadow_hits = 0
        luma_total = 0.0
        sample_count = 0
        sample_stride = 4      # 统计做 1/4 采样，够用又快

        for start in range(0, height, _STRIP_ROWS):
            stop = min(start + _STRIP_ROWS, height)
            output[start:stop] = plan.process(array[start:stop])

            sample = output[start:stop][::sample_stride, ::sample_stride]
            if sample.size:
                luma = sample @ _LUMA / np.float32(255.0)
                highlight_hits += int(np.count_nonzero(luma >= 0.999))
                shadow_hits += int(np.count_nonzero(luma <= 0.001))
                luma_total += float(luma.sum())
                sample_count += int(luma.size)

        stats = RenderStats(gain=plan.gain, applied=plan.applied())
        if sample_count:
            stats.mean_luma = luma_total / sample_count
            stats.clipped_highlights_pct = highlight_hits * 100.0 / sample_count
            stats.clipped_shadows_pct = shadow_hits * 100.0 / sample_count

        return RenderResult(image=Image.fromarray(output, mode="RGB"), stats=stats)


engine = RenderEngine()