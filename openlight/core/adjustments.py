"""调整项注册表。

这是整个软件「可被 agent 理解」的地方：每个参数只在这里描述一次，
它的名字、范围、默认值、单位、语义说明就会同时出现在：
- `app.schema` 的输出里（agent 读得到）；
- 界面面板上（人看得到）；
- 命令校验里（超范围、类型错误由它兜住）。

新增调整项的流程固定为两步：在 REGISTRY 里注册 + 在 engine.py 里实现像素运算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .color import (BAND_ATTRIBUTE_LABELS, BAND_ATTRIBUTES, BAND_CENTERS,
                    BAND_LABELS, BAND_ORDER, BAND_COLORS)
from .curves import identity_points, is_identity, normalize_points

# 界面和 schema 里的分组顺序
GROUP_ORDER = ("白平衡", "基本", "偏好", "色调曲线", "曲线", "HSL 混色器")

GROUP_LABELS = {
    "白平衡": "白平衡",
    "基本": "基本",
    "偏好": "偏好",
    "色调曲线": "色调曲线",
    "曲线": "曲线",
    "HSL 混色器": "HSL 混色器",
}


@dataclass(frozen=True)
class AdjustmentSpec:
    """一个可调参数的完整描述。"""

    id: str                      # 机器用标识符，命令里传的就是它
    label: str                   # 界面上显示的名字
    group: str                   # 面板分组
    kind: str = "scalar"         # scalar（数值）| curve（控制点）
    subgroup: str = ""           # 次级分组，HSL 混色器用它表示色带
    unit: str = ""               # 单位，如 "EV"
    minimum: float = 0.0
    maximum: float = 1.0
    default: Any = 0.0
    step: float = 1.0            # 界面步进
    precision: int = 0           # 界面显示几位小数
    description: str = ""        # 语义说明，agent 靠它理解参数含义
    agent_hint: str = ""         # 给 agent 的使用建议
    order: int = 0               # 同组内的排列顺序
    curve_channel: str | None = None   # 曲线作用的通道

    def default_value(self):
        """默认值。曲线类型的默认值是一条 45 度直线。"""
        if self.kind == "curve":
            return identity_points()
        return self.default

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "subgroup": self.subgroup,
            "kind": self.kind,
            "unit": self.unit,
            "minimum": self.minimum if self.kind == "scalar" else 0.0,
            "maximum": self.maximum if self.kind == "scalar" else 1.0,
            "default": self.default_value(),
            "step": self.step,
            "precision": self.precision,
            "description": self.description,
            "agent_hint": self.agent_hint,
            "curve_channel": self.curve_channel,
        }


@dataclass
class CurvePointError(ValueError):
    """控制点非法。"""


class AdjustmentRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, AdjustmentSpec] = {}
        self._order: list[str] = []

    # -- 注册与查询 ---------------------------------------------------------
    def register(self, spec: AdjustmentSpec) -> AdjustmentSpec:
        if spec.id in self._specs:
            raise ValueError("调整项 id 重复注册：%s" % spec.id)
        if spec.kind == "scalar":
            if not (spec.minimum <= float(spec.default) <= spec.maximum):
                raise ValueError("%s 的默认值不在范围内" % spec.id)
        elif spec.kind != "curve":
            raise ValueError("%s 的 kind 只能是 scalar 或 curve" % spec.id)
        self._specs[spec.id] = spec
        self._order.append(spec.id)
        return spec

    def get(self, adjustment_id: str) -> AdjustmentSpec:
        return self._specs[adjustment_id]

    def has(self, adjustment_id: str) -> bool:
        return adjustment_id in self._specs

    def ids(self) -> list[str]:
        return list(self._order)

    def all(self) -> list[AdjustmentSpec]:
        return [self._specs[key] for key in self._order]

    def by_group(self, group: str) -> list[AdjustmentSpec]:
        specs = [spec for spec in self.all() if spec.group == group]
        return sorted(specs, key=lambda spec: spec.order)

    def groups(self) -> list[dict]:
        """分组信息，界面和 agent 都靠它决定「怎么归类」。"""
        result: list[dict] = []
        seen = set()
        for group in GROUP_ORDER:
            specs = self.by_group(group)
            if specs:
                result.append({
                    "id": group,
                    "label": GROUP_LABELS.get(group, group),
                    "adjustments": [spec.id for spec in specs],
                })
                seen.add(group)
        for group in {spec.group for spec in self.all()} - seen:
            specs = self.by_group(group)
            result.append({"id": group, "label": group,
                           "adjustments": [spec.id for spec in specs]})
        return result

    # -- 取值处理 -----------------------------------------------------------
    def default_params(self) -> dict:
        return {spec.id: spec.default_value() for spec in self.all()}

    def coerce(self, adjustment_id: str, value: Any):
        """把外部给的值变成合法值；不合法直接抛 ValueError。"""
        spec = self.get(adjustment_id)
        if spec.kind == "curve":
            # 曲线要的是控制点。类型不对必须报错，不能悄悄当成「直线」，
            # 否则 agent 传错格式会以为设置成功了。
            if not isinstance(value, (list, tuple)):
                raise ValueError("%s 需要控制点列表，收到 %r。" % (adjustment_id, value))
            for item in value:
                if isinstance(item, dict):
                    valid = "x" in item and "y" in item
                elif isinstance(item, (list, tuple)):
                    valid = len(item) >= 2
                else:
                    valid = False
                if not valid:
                    raise ValueError("%s 的控制点格式不对：%r；"
                                     "应为 [[x, y], ...]。" % (adjustment_id, item))
            points = normalize_points(value)
            if len(points) < 2:
                raise CurvePointError("曲线至少需要 2 个控制点。")
            return points
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s 需要数字，收到 %r。" % (adjustment_id, value))
        return float(min(max(number, spec.minimum), spec.maximum))

    def clamp(self, adjustment_id: str, value: Any):
        """兼容旧名字：标量夹到范围内，曲线做规范化。"""
        return self.coerce(adjustment_id, value)

    def is_default(self, adjustment_id: str, value: Any) -> bool:
        spec = self.get(adjustment_id)
        if spec.kind == "curve":
            return is_identity(value)
        try:
            return abs(float(value) - float(spec.default)) < 1e-9
        except (TypeError, ValueError):
            return False


registry = AdjustmentRegistry()


# ---------------------------------------------------------------------------
# 注册辅助函数
# ---------------------------------------------------------------------------
def _scalar(adjustment_id: str, label: str, group: str, *, minimum: float = -100.0,
            maximum: float = 100.0, default: float = 0.0, unit: str = "",
            step: float = 1.0, precision: int = 0, description: str = "",
            agent_hint: str = "", order: int = 0, subgroup: str = "") -> AdjustmentSpec:
    return registry.register(AdjustmentSpec(
        id=adjustment_id, label=label, group=group, kind="scalar", subgroup=subgroup,
        unit=unit, minimum=minimum, maximum=maximum, default=default, step=step,
        precision=precision, description=description, agent_hint=agent_hint, order=order,
    ))


def _curve(adjustment_id: str, label: str, channel: str, *, order: int = 0,
           description: str = "", agent_hint: str = "") -> AdjustmentSpec:
    return registry.register(AdjustmentSpec(
        id=adjustment_id, label=label, group="曲线", kind="curve",
        curve_channel=channel, order=order,
        description=description, agent_hint=agent_hint,
    ))


# ---------------------------------------------------------------------------
# 白平衡
# ---------------------------------------------------------------------------
_scalar(
    "temperature", "色温", "白平衡", order=0,
    description=("白平衡的冷暖轴。-100 偏冷（加蓝，适合钨丝灯下拍的照片），"
                 "+100 偏暖（加黄橙，适合阴天、雪地）。运算在线性空间做通道增益。"),
    agent_hint=("「照片发黄、想变冷」就取负值，常见 -15 ~ -40；"
                "「照片发蓝、想变暖」取正值，常见 +15 ~ +40。一次别超过 ±50。"),
)
_scalar(
    "tint", "色调", "白平衡", order=1,
    description=("白平衡的绿-洋红轴。-100 偏绿，+100 偏洋红。"
                 "用来校正荧光灯发绿、或者画面被人为处理过后的偏色。"),
    agent_hint=("画面整体发绿取正值，发洋红取负值。常见幅度 ±10 ~ ±30，"
                "这个轴比色温更敏感，建议小步调。"),
)

# ---------------------------------------------------------------------------
# 基本
# ---------------------------------------------------------------------------
_scalar(
    "exposure", "曝光", "基本", unit="EV", minimum=-5.0, maximum=5.0,
    step=0.01, precision=2, order=0,
    description=("整体亮度调整，单位为 EV（曝光档）。+1.0 EV 表示亮度翻倍，"
                 "等同于相机快门多开一档。-1.0 EV 表示亮度减半。"
                 "运算发生在场景线性空间，等效于一次真实的曝光补偿。"),
    agent_hint=("照片偏暗就取正值（常见 +0.3 ~ +1.5），偏亮就取负值。"
                "不要一次给到 ±5，先小幅调整再看返回的 mean_luma。"),
)
_scalar(
    "contrast", "对比度", "基本", order=1,
    description=("以中灰为支点，同时压暗暗部、提亮亮部，让画面更「实」。"
                 "曲线是平滑的三次 S 形，不会像线性拉伸那样产生硬断层，"
                 "0 和 1 两个端点固定不动。"),
    agent_hint=("画面发灰、层次不清时取正值（常见 +15 ~ +40）；"
                "反差过大、想要柔和感时取负值。"),
)
_scalar(
    "highlights", "高光", "基本", order=2,
    description=("只作用于画面亮部的亮度。负值把过亮的地方拉回来（找天空细节），"
                 "正值让亮部更亮。对暗部几乎没有影响。"),
    agent_hint=("天空过曝、白衣服没细节：给 -30 ~ -70。"
                "画面「平」、想要亮部跳出来：给 +20 ~ +40。"),
)
_scalar(
    "shadows", "阴影", "基本", order=3,
    description=("只作用于画面暗部的亮度。正值提亮暗部找回细节，"
                 "负值压暗暗部让画面更沉稳。对亮部几乎没有影响。"),
    agent_hint=("背光、暗部糊成一团：给 +30 ~ +70。"
                "想让画面更有「厚」感：给 -20 ~ -40。"),
)
_scalar(
    "whites", "白色色阶", "基本", order=4,
    description=("调整白点位置。正值把最亮处继续往白推（画面更通透，可能削波），"
                 "负值把白点往下拉（原本的纯白变成浅灰，画面更柔和）。"),
    agent_hint=("画面「不够亮、不够白」时给 +10 ~ +30；"
                "想要胶片那种柔和不刺眼的高光时给 -10 ~ -30。"),
)
_scalar(
    "blacks", "黑色色阶", "基本", order=5,
    description=("调整黑点位置。负值把最暗处压成纯黑（更通透、更硬），"
                 "正值抬起最暗处（灰雾感、胶片感）。"),
    agent_hint=("雾蒙蒙、缺少黑场：给 -10 ~ -30。"
                "画面太硬、暗部死黑：给 +10 ~ +30。"),
)

# ---------------------------------------------------------------------------
# 偏好
# ---------------------------------------------------------------------------
_scalar(
    "vibrance", "自然饱和度", "偏好", order=0,
    description=("优先提升「还不够鲜艳」的颜色，已经很饱和的区域基本不动，"
                 "对肤色的影响也比饱和度小。适合让画面整体有生气而不假。"),
    agent_hint=("人像、肤色为主：优先用它而不是「饱和度」，常见 +10 ~ +35。"),
)
_scalar(
    "saturation", "饱和度", "偏好", order=1,
    description=("所有颜色等比例提升或降低。+100 接近两倍饱和，-100 接近灰阶。"),
    agent_hint=("需要「更浓」的统一效果时用，常见 ±10 ~ ±30；"
                "要完全去色取 -100。注意它会同时放大肤色偏色。"),
)

# ---------------------------------------------------------------------------
# 色调曲线（分区滑块）
# ---------------------------------------------------------------------------
_scalar(
    "tone_highlights", "高光区", "色调曲线", order=0,
    description=("色调曲线的高光区（画面最亮的那一段）。四个分区滑块叠加成一条"
                 "平滑曲线，权重用升余弦构造，相邻区之间自然过渡。"),
    agent_hint="和「高光」滑块类似但更柔和，常用于整体风格微调，幅度 ±10 ~ ±30。",
)
_scalar(
    "tone_lights", "亮调区", "色调曲线", order=1,
    description="色调曲线的亮调区（偏亮但不是最亮的一段）。",
    agent_hint="控制「亮部层次」，+20 能让中间偏亮的地方更通透。",
)
_scalar(
    "tone_darks", "暗调区", "色调曲线", order=2,
    description="色调曲线的暗调区（偏暗但不是最暗的一段）。",
    agent_hint="控制「暗部层次」，-20 能让暗部更沉，+20 能抬起暗部细节。",
)
_scalar(
    "tone_shadows", "阴影区", "色调曲线", order=3,
    description="色调曲线的最暗区。",
    agent_hint="比「阴影」滑块更靠黑端，用于控制黑场附近的过渡。",
)

# ---------------------------------------------------------------------------
# 曲线（点曲线）
# ---------------------------------------------------------------------------
_CURVE_DESC = ("控制点列表，格式 [[x, y], ...]，x 和 y 都在 0~1，"
               "x 必须递增，至少 2 个点。默认是 [[0,0],[1,1]]（直线，不改变画面）。"
               "插值用单调三次样条，永远不会过冲。")
_curve(
    "curve_rgb", "RGB 综合", "rgb", order=0,
    description="作用于整体亮度的曲线，先于通道曲线执行。" + _CURVE_DESC,
    agent_hint=("提亮中间调：[[0,0],[0.5,0.6],[1,1]]；"
                "加对比 S 形：[[0,0],[0.25,0.18],[0.75,0.82],[1,1]]；"
                "压暗白场做出褪色感：[[0,0],[1,0.9]]；"
                "提黑做胶片感：[[0,0.08],[1,1]]。"),
)
_curve(
    "curve_red", "红通道", "red", order=1,
    description="只作用于红通道。在 RGB 曲线之后执行。" + _CURVE_DESC,
    agent_hint="往右上抬整体偏红/偏暖，往下压偏青。常用于冷暖分离的调色。",
)
_curve(
    "curve_green", "绿通道", "green", order=2,
    description="只作用于绿通道。在 RGB 曲线之后执行。" + _CURVE_DESC,
    agent_hint="往上抬偏绿，往下压偏洋红，常用于校正偏色。",
)
_curve(
    "curve_blue", "蓝通道", "blue", order=3,
    description="只作用于蓝通道。在 RGB 曲线之后执行。" + _CURVE_DESC,
    agent_hint="暗部抬蓝 + 亮部压蓝是经典的「青橙」方向；反之偏黄。",
)

# ---------------------------------------------------------------------------
# HSL 混色器：8 个色带 x 3 个属性
# ---------------------------------------------------------------------------
_HSL_DESCRIPTIONS = {
    "hue": ("把该色系整体往相邻色相偏移（±100 约等于 ±30 度）。"
            "用来微调某个颜色的「味道」，比如让黄色更偏橙。"),
    "sat": "只调整该色系的鲜艳程度，其他颜色不受影响。",
    "lum": "只调整该色系的明暗，其他颜色不受影响；接近灰的像素不会被动到。",
}
_HSL_HINTS = {
    "hue": "幅度要小，常见 ±10 ~ ±25；大了会出现色带断裂感。",
    "sat": "常见 ±10 ~ ±40。压低某个杂色（比如草地的黄绿）是常见用法。",
    "lum": "常见 ±10 ~ ±40。压暗蓝色天空、提亮肤色都用它。",
}

_band_index = 0
for _band in BAND_ORDER:
    for _attribute in BAND_ATTRIBUTES:
        _scalar(
            "hsl_%s_%s" % (_band, _attribute),
            BAND_ATTRIBUTE_LABELS[_attribute],
            "HSL 混色器",
            subgroup=BAND_LABELS[_band],
            order=_band_index * 10 + BAND_ATTRIBUTES.index(_attribute),
            description=("%s（%s色系）。%s" % (BAND_ATTRIBUTE_LABELS[_attribute],
                                              BAND_LABELS[_band],
                                              _HSL_DESCRIPTIONS[_attribute])),
            agent_hint=_HSL_HINTS[_attribute],
        )
    _band_index += 1


# ---------------------------------------------------------------------------
# 给界面和 schema 用的常量
# ---------------------------------------------------------------------------
BAND_META = [
    {"id": band, "label": BAND_LABELS[band], "color": BAND_COLORS[band],
     "center_deg": round(BAND_CENTERS[band] * 360.0, 3)}
    for band in BAND_ORDER
]

__all__ = [
    "AdjustmentSpec", "AdjustmentRegistry", "registry", "GROUP_ORDER", "GROUP_LABELS",
    "CurvePointError",
]