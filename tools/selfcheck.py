"""内核自检：不开界面，验证「打开 -> 调整 42 项参数 -> 预览 -> 撤销 -> 导出」整条链路。

改动内核或接口后跑一遍：
    python tools/selfcheck.py
"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Windows 控制台默认 GBK，中文输出会乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from openlight.api.catalog import command_catalog  # noqa: E402
from openlight.core import engine  # noqa: E402
from openlight.core.adjustments import registry  # noqa: E402
from openlight.core.color import SRGB_TO_LINEAR, hsl_to_rgb, rgb_to_hsl  # noqa: E402
from openlight.core.curves import is_identity, monotone_lut, normalize_points  # noqa: E402
from openlight.core.session import Session  # noqa: E402

RESULTS: list[tuple[bool, str]] = []
WIDTH, HEIGHT = 1200, 800


def check(condition: bool, label: str) -> None:
    RESULTS.append((bool(condition), label))
    print(("  [ok]   " if condition else "  [FAIL] ") + label)


def section(text: str) -> None:
    print("\n" + text)


def make_test_image(path: Path) -> None:
    """三段式样张：灰阶 / 高饱和色环 / 低饱和色环。

    灰阶用来验证曝光倍数和色阶；两段色环覆盖 HSL 混色器的全部色带，
    让每个色带滑块都能被验证到。
    """
    rgb = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)

    ramp = np.linspace(0.02, 0.96, 400, dtype=np.float32)
    rgb[:, :400] = ramp[None, :, None]

    hue = np.linspace(0.0, 1.0, 400, endpoint=False, dtype=np.float32)
    hue = np.tile(hue, (HEIGHT, 1))
    rgb[:, 400:800] = hsl_to_rgb(hue, np.full_like(hue, 0.95), np.full_like(hue, 0.5))

    muted_hue = np.tile(np.linspace(0.0, 1.0, 400, endpoint=False, dtype=np.float32),
                        (HEIGHT, 1))
    rgb[:, 800:] = hsl_to_rgb(muted_hue, np.full_like(muted_hue, 0.35),
                              np.full_like(muted_hue, 0.62))

    Image.fromarray((rgb * 255.0 + 0.5).astype(np.uint8), "RGB").save(path, quality=100)


def mean_of(image: Image.Image) -> float:
    arr = np.asarray(image.convert("L"), dtype=np.float32)
    return float(arr.mean())


def diff_of(a: Image.Image, b: Image.Image) -> float:
    left = np.asarray(a.convert("RGB"), dtype=np.float32)
    right = np.asarray(b.convert("RGB"), dtype=np.float32)
    return float(np.abs(left - right).mean())


def render(params: dict, source_path: Path) -> Image.Image:
    with Image.open(source_path) as handle:
        image = handle.convert("RGB")
    return engine.engine.render(image, params).image


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="openlight-selfcheck-"))
    source = workdir / "sample.jpg"
    make_test_image(source)
    print(f"临时目录：{workdir}\n")

    session = Session()

    # ------------------------------------------------------------------
    section("1) schema 自述文档")
    schema = session.execute({"cmd": "app.schema"})
    check(schema["ok"], "app.schema 可以调用")
    result = schema["result"]
    catalog_names = {item["cmd"] for item in result["commands"]}
    handlers = set(session._handlers.keys())  # noqa: SLF001
    missing = handlers - catalog_names
    check(not missing, f"所有命令都在 catalog 里登记（缺失：{sorted(missing) or '无'}）")
    check(result["parameter_count"] == 42, f"暴露 42 个调整项（实际 {result['parameter_count']}）")
    check(len(result["groups"]) == 6,
          f"分成 6 组：{'、'.join(g['id'] for g in result['groups'])}")
    check(all(item["example"] and "cmd" in item["example"] for item in result["commands"]),
          "每条命令都带可直接照抄的示例")
    check(all(spec.get("agent_hint") for spec in result["adjustments"]),
          "每个调整项都带 agent_hint（agent 靠它知道怎么调）")

    expected_groups = {
        "白平衡": ["temperature", "tint"],
        "基本": ["exposure", "contrast", "highlights", "shadows", "whites", "blacks"],
        "偏好": ["vibrance", "saturation"],
        "色调曲线": ["tone_highlights", "tone_lights", "tone_darks", "tone_shadows"],
        "曲线": ["curve_rgb", "curve_red", "curve_green", "curve_blue"],
    }
    ok = True
    for group, ids in expected_groups.items():
        actual = [spec.id for spec in registry.by_group(group)]
        if actual != ids:
            ok = False
            print(f"     {group}: {actual}")
    hsl_ids = [spec.id for spec in registry.by_group("HSL 混色器")]
    if len(hsl_ids) != 24:
        ok = False
        print(f"     HSL 混色器: {len(hsl_ids)} 项")
    check(ok, "参数分组与命名符合设计（白平衡2 / 基本6 / 偏好2 / 色调曲线4 / 曲线4 / HSL24）")
    check(all(registry.get(spec.id).kind == "curve" for spec in registry.by_group("曲线")),
          "曲线组的 4 个参数都是 kind=curve")
    exposure = registry.get("exposure")
    check(exposure.minimum == -5.0 and exposure.maximum == 5.0, "曝光范围 -5 ~ +5 EV")
    check(registry.get("curve_rgb").default_value() == [[0.0, 0.0], [1.0, 1.0]],
          "曲线的默认值是 45 度直线")

    # ------------------------------------------------------------------
    section("2) 打开照片")
    opened = session.execute({"cmd": "document.open", "params": {"path": str(source)}})
    check(opened["ok"], "document.open 成功")
    check(opened["result"]["document"]["width"] == WIDTH, "读到正确宽度")
    check(len(opened["result"]["document"]["adjustments"]) == 42,
          "打开后每个调整项都有默认值")

    # ------------------------------------------------------------------
    section("3) 曝光运算（线性空间，可算准）")
    before = session.preview_png(WIDTH)
    check(bool(before), "能渲染预览 PNG")
    base_mean = mean_of(Image.open(io.BytesIO(before)))

    plus_one = session.execute({"cmd": "adjustment.set",
                                "params": {"id": "exposure", "value": 1.0}})
    check(plus_one["result"]["value"] == 1.0 and not plus_one["result"]["clamped"],
          "设置曝光 +1EV")
    after_mean = mean_of(Image.open(io.BytesIO(session.preview_png(WIDTH))))
    check(after_mean > base_mean + 15, f"+1EV 后整体变亮（{base_mean:.1f} -> {after_mean:.1f}）")

    # 灰阶段的每个像素在线性空间应该正好翻倍（sRGB 解码 -> x2 -> 编码）
    gray_before = np.asarray(render(registry.default_params(), source))[:, :400, 0]
    gray_after = np.asarray(render({**registry.default_params(), "exposure": 1.0},
                                   source))[:, :400, 0]
    linear_before = SRGB_TO_LINEAR[gray_before]
    linear_after = SRGB_TO_LINEAR[gray_after]
    # 只比中低段：8bit 输出在最暗处 1 个码值就是很大的相对误差
    sample = (linear_before > 0.03) & (linear_before < 0.25)
    ratio = linear_after[sample] / linear_before[sample]
    check(bool(np.all(np.abs(ratio - 2.0) < 0.06)),
          f"+1EV 等价于线性亮度翻倍（逐个像素验证，比值 {ratio.min():.3f} ~ {ratio.max():.3f}）")

    state = session.execute({"cmd": "document.state"})
    check(state["result"]["document"]["active_adjustments"].get("exposure") == 1.0,
          "state 里能看出当前偏离默认值的参数")
    session.execute({"cmd": "adjustment.reset", "params": {"id": "exposure"}})

    # ------------------------------------------------------------------
    section("4) 42 个参数逐个生效")
    defaults = registry.default_params()
    reference = render(defaults, source)
    silent: list[str] = []
    slowest = (0.0, "")
    for spec in registry.all():
        params = dict(defaults)
        if spec.kind == "curve":
            params[spec.id] = [[0.0, 0.0], [0.5, 0.62], [1.0, 1.0]]
        else:
            params[spec.id] = spec.maximum * 0.6
        started = time.perf_counter()
        image = render(params, source)
        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > slowest[0]:
            slowest = (elapsed, spec.id)
        delta = diff_of(reference, image)
        if delta < 0.05:
            silent.append(f"{spec.id}({delta:.3f})")
    check(not silent, f"每个参数都能改变画面（无变化的：{silent or '无'}）")
    print(f"     单项渲染最慢：{slowest[1]} {slowest[0]:.0f}ms（{WIDTH}x{HEIGHT}）")

    # 全参数一起上：既检查不崩，也量一下耗时
    full = dict(defaults)
    full.update({
        "temperature": 30, "tint": -12, "exposure": 0.4, "contrast": 35,
        "highlights": -45, "shadows": 40, "whites": 18, "blacks": -20,
        "vibrance": 25, "saturation": -8,
        "tone_highlights": 20, "tone_lights": 12, "tone_darks": -15, "tone_shadows": 10,
        "curve_rgb": [[0, 0], [0.25, 0.18], [0.75, 0.85], [1, 1]],
        "curve_red": [[0, 0.02], [0.5, 0.53], [1, 1]],
        "curve_green": [[0, 0], [1, 0.97]],
        "curve_blue": [[0, 0.05], [1, 0.95]],
    })
    for band in ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"):
        full[f"hsl_{band}_sat"] = 15
        full[f"hsl_{band}_lum"] = -10
        full[f"hsl_{band}_hue"] = 8
    started = time.perf_counter()
    with Image.open(source) as handle:
        rendered = engine.engine.render(handle.convert("RGB"), full)
    full_ms = (time.perf_counter() - started) * 1000
    check(len(rendered.stats.applied) == 9,
          f"全参数渲染报告实际生效的处理级：{'、'.join(rendered.stats.applied)}")
    print(f"     全参数渲染 {WIDTH}x{HEIGHT}：{full_ms:.0f}ms")

    # 默认值必须逐像素等于原图
    with Image.open(source) as handle:
        original = handle.convert("RGB")
    identity = engine.engine.render(original, defaults)
    check(identity.stats.identity and np.array_equal(np.asarray(original),
                                                    np.asarray(identity.image)),
          "全部默认时逐像素等于原图（identity）")

    # ------------------------------------------------------------------
    section("5) 曲线")
    lut = monotone_lut([[0, 0], [0.25, 0.18], [0.75, 0.82], [1, 1]])
    check(bool(np.all(np.diff(lut) >= -1e-6)), "点曲线单调不减（不会出现反相断层）")
    check(lut[0] == 0.0 and abs(float(lut[-1]) - 1.0) < 1e-6, "曲线端点固定在 0 和 1")
    steep = monotone_lut([[0, 0], [0.49, 0.2], [0.51, 0.8], [1, 1]])
    check(steep.max() <= 1.0 + 1e-6 and bool(np.all(np.diff(steep) >= -1e-6)),
          "极端控制点也不会过冲（Fritsch-Carlson 限制）")
    check(is_identity([[0, 0], [1, 1]]) and not is_identity([[0, 0], [1, 0.9]]),
          "能识别默认直线，从而整段跳过渲染")
    check(normalize_points([[1, 1], [0.5, 0.7], [0.2, 0.1], [0.5, 0.9]])
          == [[0.2, 0.1], [0.5, 0.7], [1.0, 1.0]],
          "控制点会被排序、去重、夹到 0~1")

    curve_result = session.execute({"cmd": "curve.set_points",
                                    "params": {"id": "curve_rgb",
                                               "points": [[0, 0], [0.5, 0.62], [1, 1]]}})
    check(curve_result["ok"] and curve_result["result"]["points"][1] == [0.5, 0.62],
          "curve.set_points 能设置控制点")
    check(session.document.params["curve_rgb"] == [[0.0, 0.0], [0.5, 0.62], [1.0, 1.0]],
          "曲线参数进了文档状态")
    bad_curve = session.execute({"cmd": "adjustment.set",
                                 "params": {"id": "curve_red", "value": "不是控制点"}})
    check(not bad_curve["ok"] and bad_curve["error"]["code"] == "bad_request",
          "非法的曲线取值返回结构化错误")
    not_curve = session.execute({"cmd": "curve.set_points",
                                 "params": {"id": "exposure", "points": [[0, 0], [1, 1]]}})
    check(not not_curve["ok"] and not_curve["error"]["code"] == "not_a_curve",
          "对非曲线参数调用 curve.set_points 会被拒绝")
    lifted = render({**defaults, "curve_rgb": session.document.params["curve_rgb"]}, source)
    check(mean_of(lifted) > mean_of(reference), "提亮曲线确实提亮了画面")
    session.execute({"cmd": "adjustment.reset", "params": {"id": "curve_rgb"}})

    # ------------------------------------------------------------------
    section("6) HSL 混色器")
    gray = render({**defaults, "saturation": -100}, source)
    gray_arr = np.asarray(gray)
    check(int((gray_arr.max(axis=2).astype(int) - gray_arr.min(axis=2).astype(int)).max()) == 0,
          "饱和度 -100 得到真正的灰阶")

    hue = np.linspace(0, 1, 2048, endpoint=False, dtype=np.float32)
    sat = np.full_like(hue, 0.7)
    lum = np.full_like(hue, 0.5)
    round_trip = hsl_to_rgb(*rgb_to_hsl(hsl_to_rgb(hue, sat, lum)))
    check(float(np.abs(round_trip - hsl_to_rgb(hue, sat, lum)).max()) < 1e-4,
          "HSL 往返误差可忽略")

    only_blue = render({**defaults, "hsl_blue_sat": -100}, source)
    left = np.abs(np.asarray(only_blue, np.float32)[:, 380:420]
                  - np.asarray(reference, np.float32)[:, 380:420]).mean()
    check(left < 1.0, f"蓝色带滑块不影响黄/洋红附近的颜色（差异 {left:.2f}）")
    blue_zone = np.abs(np.asarray(only_blue, np.float32)[:, 400:800]
                       - np.asarray(reference, np.float32)[:, 400:800]).mean()
    check(blue_zone > 1.0, f"但它重写了蓝色区域（差异 {blue_zone:.2f}）")

    gray_patch = render({**defaults, "hsl_blue_lum": 100, "hsl_red_lum": 100}, source)
    neutral_delta = np.abs(np.asarray(gray_patch, np.float32)[:, :400]
                           - np.asarray(reference, np.float32)[:, :400]).mean()
    check(neutral_delta < 0.5,
          f"明亮度滑块不会污染灰阶（灰像素没有色相，差异 {neutral_delta:.2f}）")

    # ------------------------------------------------------------------
    section("7) 范围与错误处理")
    clamped = session.execute({"cmd": "adjustment.set",
                               "params": {"id": "exposure", "value": 99}})
    check(clamped["result"]["clamped"] and clamped["result"]["value"] == 5.0,
          "超出范围的取值被夹到边界并标记 clamped")
    normalized = session.execute({"cmd": "adjustment.set",
                                  "params": {"id": "curve_rgb", "value": [[1, 1], [0, 0]]}})
    check(normalized["result"]["clamped"] and normalized["result"]["value"] == [[0.0, 0.0], [1.0, 1.0]],
          "曲线控制点被规范化后标记 clamped")
    unknown_adjustment = session.execute({"cmd": "adjustment.set",
                                          "params": {"id": "contrastt", "value": 1}})
    check(not unknown_adjustment["ok"]
          and unknown_adjustment["error"]["code"] == "unknown_adjustment",
          "未知调整项返回结构化错误，并给出可用清单")
    check(bool(unknown_adjustment["error"].get("hint")), "错误里带 hint，agent 能自我纠正")
    unknown = session.execute({"cmd": "no.such.command"})
    check(not unknown["ok"] and unknown["error"]["code"] == "unknown_command",
          "未知命令返回结构化错误")
    session.execute({"cmd": "adjustments.reset_all"})

    # ------------------------------------------------------------------
    section("8) 批量设置 / 历史 / 撤销")
    steps_before = len(session.history.entries())
    batch = session.execute({"cmd": "adjustments.set_batch",
                             "params": {"adjustments": {
                                 "exposure": 0.5, "temperature": 20, "vibrance": 15,
                                 "curve_rgb": [[0, 0], [0.5, 0.55], [1, 1]]}}})
    check(batch["ok"] and batch["result"]["applied"]["temperature"] == 20,
          "批量设置一次写入 4 个参数")
    check(len(session.history.entries()) == steps_before + 1, "批量设置只占一个历史步骤")

    session.execute({"cmd": "adjustment.set", "params": {"id": "exposure", "value": 1.25}})
    undo = session.execute({"cmd": "history.undo"})
    check(undo["ok"] and undo["result"]["parameters"]["exposure"] == 0.5, "撤销回到上一步")
    redo = session.execute({"cmd": "history.redo"})
    check(redo["ok"] and redo["result"]["parameters"]["exposure"] == 1.25, "重做回到下一步")
    hist = session.execute({"cmd": "history.state"})
    sources = {e["source"] for e in hist["result"]["entries"]}
    check("agent" in sources, "历史记录里保留了操作来源")
    labels = [e["label"] for e in hist["result"]["entries"]]
    check(any("曲线" in label for label in labels),
          f"历史标签对曲线是可读的：{labels[-1]}")

    # ------------------------------------------------------------------
    section("9) base64 预览接口")
    preview = session.execute({"cmd": "preview.get", "params": {"max_side": 256}})
    check(preview["ok"], "preview.get 成功")
    raw = base64.b64decode(preview["result"]["data_base64"])
    image = Image.open(io.BytesIO(raw))
    check(max(image.size) == 256, f"预览图长边被限制为 256（实际 {image.size}）")
    stats = preview["result"]["stats"]
    check({"mean_luma", "clipped_highlights_pct", "clipped_shadows_pct", "applied"} <= set(stats),
          "预览返回画面统计，供 agent 判断是否调过头")
    check(stats["applied"], f"stats.applied 报告生效的处理级：{stats['applied']}")
    cached = session.execute({"cmd": "preview.get", "params": {"max_side": 256}})
    check(cached["result"]["data_base64"] == preview["result"]["data_base64"],
          "相同参数命中缓存，结果稳定可复现")

    # ------------------------------------------------------------------
    section("10) 导出")
    target = workdir / "out.jpg"
    exported = session.execute({"cmd": "export.image", "params": {"path": str(target)}})
    check(exported["ok"] and target.exists(), "导出为文件成功")
    dup = session.execute({"cmd": "export.image", "params": {"path": str(target)}})
    check(not dup["ok"] and dup["error"]["code"] == "file_exists", "默认不覆盖已存在文件")
    self_overwrite = session.execute({"cmd": "export.image", "params": {"path": str(source)}})
    check(not self_overwrite["ok"], "拒绝覆盖原始照片")

    # ------------------------------------------------------------------
    section("11) sidecar 配方（含曲线）")
    saved = session.execute({"cmd": "recipe.save"})
    check(saved["ok"] and Path(saved["result"]["path"]).exists(), "写出 sidecar")
    session.execute({"cmd": "adjustments.reset_all"})
    session.execute({"cmd": "recipe.reload"})
    reloaded = session.execute({"cmd": "document.state"})
    params = reloaded["result"]["document"]["adjustments"]
    check(params["exposure"] == 1.25, "sidecar 载入后恢复标量参数")
    check(params["curve_rgb"] == [[0.0, 0.0], [0.5, 0.55], [1.0, 1.0]],
          "sidecar 载入后恢复曲线控制点")
    check(params["hsl_blue_lum"] == 0 or params["hsl_blue_lum"] == 0.0,
          "sidecar 里没有的旧字段会补上默认值")

    # ------------------------------------------------------------------
    section("12) 事件日志")
    events = session.execute({"cmd": "events.list", "params": {"since": 0}})
    types = {e["type"] for e in events["result"]["events"]}
    check("adjustment.changed" in types and "image.exported" in types,
          "关键操作都写进了事件日志")
    tail = session.execute({"cmd": "events.list",
                            "params": {"since": events["result"]["last_seq"] - 1}})
    check(tail["result"]["count"] == 1, "since 参数能只取增量事件")

    # ------------------------------------------------------------------
    section("13) 预览耗时（拖动滑块要跟手）")
    with Image.open(source) as handle:
        photo = handle.convert("RGB")
    timings = [
        ("曝光（最常见）", {"exposure": 0.6}),
        ("基本六项", {"exposure": 0.4, "contrast": 25, "highlights": -40, "shadows": 30,
                      "whites": 10, "blacks": -12}),
        ("全部 42 项", full),
    ]
    for label, params in timings:
        merged = {**defaults, **params}
        started = time.perf_counter()
        engine.engine.render(photo, merged, max_side=1400)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"     {label:12s} 长边 1400 预览：{elapsed:5.0f}ms")
    dragged = {**defaults, "exposure": 0.6}
    started = time.perf_counter()
    engine.engine.render(photo, dragged, max_side=900)
    print(f"     {'拖动中预览':12s} 长边 900  预览：{(time.perf_counter() - started) * 1000:5.0f}ms")

    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"失败 {len(failed)} 项：")
        for label in failed:
            print("  - " + label)
        return 1
    print(f"全部通过（{len(RESULTS)} 项）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())