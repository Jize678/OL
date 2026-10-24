"""MCP server（stdio）—— 把 openLight 直接变成 agent 的工具箱。

挂到支持 MCP 的 agent（Codex / Claude Desktop 等）之后，agent 就能：
打开照片、调曝光、看图、导出。整个过程和人在界面上操作是同一套命令。

刻意只用标准库实现 JSON-RPC，不依赖 mcp SDK，避免给用户增加安装负担。

运行：python -m openlight.mcp
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request

from .. import APP_NAME, __version__
from .server import resolve_base_url as resolve_service_url

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_PORT = 8765


# ---------------------------------------------------------------------------
# 与软件通信
# ---------------------------------------------------------------------------
def base_url() -> str:
    return resolve_service_url() or f"http://127.0.0.1:{DEFAULT_PORT}"


def call(cmd: str, params: dict | None = None, timeout: float = 120.0) -> tuple[dict | None, str | None]:
    """调用软件。返回 (payload, error_text)。"""
    request = urllib.request.Request(
        f"{base_url()}/command",
        data=json.dumps({"cmd": cmd, "params": params or {}}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), None
        except Exception:
            return None, raw
    except Exception as exc:
        return None, (
            f"连不上 openLight（{base_url()}）：{exc}\n"
            "请先启动软件：python run_gui.py 或 python run_api.py"
        )


def fetch_preview(max_side: int) -> tuple[bytes | None, str | None]:
    try:
        with urllib.request.urlopen(f"{base_url()}/preview?max_side={max_side}", timeout=120) as r:
            return r.read(), None
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
def _text(message: str) -> dict:
    return {"type": "text", "text": message}


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "openlight_status",
            "description": "查看 openLight 是否在运行、当前打开的照片、当前生效的调整和可撤销步数。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "openlight_open_image",
            "description": "在 openLight 里打开一张本地照片作为当前编辑对象。",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "照片的绝对路径"}},
                "required": ["path"],
            },
        },
        {
            "name": "openlight_schema",
            "description": (
                "读取这个软件的自述文档：全部调整项（42 个，分 6 组）的名字、范围、默认值、"
                "语义和使用建议，以及全部命令清单。调参数之前先看一眼，比瞎试快得多。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "group": {"type": "string",
                              "description": "只看某一组：白平衡 / 基本 / 偏好 / 色调曲线 / "
                                             "曲线 / HSL 混色器；省略则返回全部。"}
                },
            },
        },
        {
            "name": "openlight_set_adjustment",
            "description": (
                "设置任意一个调整项。标量参数传数字，曲线参数传控制点列表。"
                "id 和取值范围的权威列表用 openlight_schema 查。"
                "例：{\"id\":\"contrast\",\"value\":25} / "
                "{\"id\":\"curve_rgb\",\"value\":[[0,0],[0.5,0.6],[1,1]]}。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "调整项 id，例如 exposure / contrast / hsl_blue_lum"},
                    "value": {"type": ["number", "array"],
                              "description": "数值，或曲线的控制点 [[x,y],...]"},
                },
                "required": ["id", "value"],
            },
        },
        {
            "name": "openlight_set_curve",
            "description": (
                "设置某条点曲线的形状。x 是输入亮度、y 是输出亮度，都在 0~1 之间，x 必须递增。"
                "默认 [[0,0],[1,1]] 是直线（不改变画面）。"
                "提亮中间调：[[0,0],[0.5,0.6],[1,1]]；加对比 S 形："
                "[[0,0],[0.25,0.18],[0.75,0.82],[1,1]]。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "default": "curve_rgb",
                           "description": "curve_rgb / curve_red / curve_green / curve_blue"},
                    "points": {"type": "array", "items": {"type": "array"},
                               "description": "控制点列表 [[x,y], ...]"},
                },
                "required": ["points"],
            },
        },
        {
            "name": "openlight_adjust",
            "description": (
                "一次设置多个调整项（只占一步历史）。做「整体风格」时用它："
                "例如 {\"exposure\":0.3,\"contrast\":20,\"temperature\":-15,\"vibrance\":20}。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "adjustments": {"type": "object",
                                    "description": "调整项 id 到值的映射"},
                },
                "required": ["adjustments"],
            },
        },
        {
            "name": "openlight_set_exposure",
            "description": (
                "设置曝光补偿，单位 EV，范围 -5 ~ +5。+1.0 表示亮度翻倍。"
                "返回里带画面统计（高光/暗部削波比例），数值过高说明调过头了。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "number", "description": "曝光值，单位 EV"}},
                "required": ["value"],
            },
        },
        {
            "name": "openlight_reset",
            "description": "把某个调整项（默认曝光）恢复到默认值。",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string", "default": "exposure"}},
            },
        },
        {
            "name": "openlight_undo",
            "description": "撤销上一步调整。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "openlight_redo",
            "description": "重做被撤销的调整。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "openlight_see_image",
            "description": (
                "查看当前调整后的画面（返回一张图片）。用来判断调整是否合适，"
                "可以反复调用，配合 openlight_adjust 迭代。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "max_side": {"type": "integer", "default": 1024,
                                 "description": "图片长边像素，越小越省上下文。"}
                },
            },
        },
        {
            "name": "openlight_export",
            "description": "按当前调整把全分辨率成品导出到文件。默认不覆盖已有文件，也不会覆盖原图。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "输出路径，省略则写成 <原名>_openlight.<后缀>"},
                    "format": {"type": "string", "description": "jpg / png / tif / webp"},
                    "quality": {"type": "integer", "description": "JPEG/WEBP 质量 1-100"},
                    "overwrite": {"type": "boolean", "default": False},
                },
            },
        },
        {
            "name": "openlight_runs",
            "description": "查看最近发生过的操作（含用户在界面上的手动操作），用于保持同步。",
            "inputSchema": {
                "type": "object",
                "properties": {"since": {"type": "integer", "default": 0},
                               "limit": {"type": "integer", "default": 20}},
            },
        },
    ]


def invoke_tool(name: str, arguments: dict) -> dict:
    """执行工具，返回 MCP 的 content 数组。"""
    if name == "openlight_status":
        payload, error = call("document.state")
        if error:
            return {"isError": True, "content": [_text(error)]}
        if not payload.get("ok"):
            return {"isError": True, "content": [_text(json.dumps(payload.get("error"), ensure_ascii=False))]}
        document = payload["result"].get("document")
        state = payload.get("state") or {}
        lines = [f"openLight 正在运行：{base_url()}"]
        if document:
            lines.append(
                f"当前照片：{document['filename']}（{document['width']}x{document['height']}）"
            )
            active = document.get("active_adjustments") or {}
            lines.append("当前调整：" + (", ".join(f"{k}={v}" for k, v in active.items()) or "全部默认"))
            exif = document.get("meta", {}).get("exif") or {}
            if exif:
                lines.append("拍摄信息：" + json.dumps(exif, ensure_ascii=False))
        else:
            lines.append("当前没有打开照片，请先调用 openlight_open_image。")
        lines.append(f"可撤销：{state.get('can_undo')}，可重做：{state.get('can_redo')}")
        return {"content": [_text("\n".join(lines))]}

    if name == "openlight_open_image":
        payload, error = call("document.open", {"path": arguments.get("path")})
        return _simple(payload, error, "已打开")

    if name == "openlight_schema":
        payload, error = call("app.schema")
        if error or not payload.get("ok"):
            return _simple(payload, error, "")
        result = payload["result"]
        wanted = arguments.get("group")
        specs = {spec["id"]: spec for spec in result["adjustments"]}
        lines = [f"openLight 共 {result.get('parameter_count')} 个调整项，"
                 f"分 {len(result.get('groups', []))} 组。"]
        for group in result.get("groups", []):
            if wanted and group["id"] != wanted:
                continue
            lines.append(f"\n【{group['id']}】")
            for adjustment_id in group["adjustments"]:
                spec = specs[adjustment_id]
                if spec["kind"] == "curve":
                    lines.append(f"  {spec['id']}（{spec['label']}，曲线）：{spec['description']}")
                else:
                    unit = spec["unit"] or ""
                    lines.append(
                        f"  {spec['id']}（{spec['label']}，{spec['minimum']:g}~"
                        f"{spec['maximum']:g}{unit}）：{spec['agent_hint']}")
        lines.append("\n命令：" + "、".join(item["cmd"] for item in result["commands"]))
        return {"content": [_text("\n".join(lines))]}

    if name == "openlight_set_adjustment":
        payload, error = call("adjustment.set", {"id": arguments.get("id"),
                                                 "value": arguments.get("value")})
        return _adjustment_receipt(payload, error)

    if name == "openlight_set_curve":
        payload, error = call("curve.set_points", {
            "id": arguments.get("id") or "curve_rgb",
            "points": arguments.get("points"),
        })
        if error or not payload.get("ok"):
            return _simple(payload, error, "")
        result = payload["result"]
        return {"content": [_text(
            f"{result['id']} 现在有 {len(result['points'])} 个控制点："
            f"{json.dumps(result['points'], ensure_ascii=False)}"
        )]}

    if name == "openlight_adjust":
        payload, error = call("adjustments.set_batch",
                              {"adjustments": arguments.get("adjustments") or {}})
        if error or not payload.get("ok"):
            return _simple(payload, error, "")
        result = payload["result"]
        active = payload.get("state", {}).get("document", {}).get("active_adjustments", {})
        return {"content": [_text(
            "已一次设置：" + json.dumps(result["applied"], ensure_ascii=False)
            + "\n当前生效的调整：" + json.dumps(active, ensure_ascii=False)
        )]}

    if name == "openlight_set_exposure":
        payload, error = call("adjustment.set", {"id": "exposure", "value": arguments.get("value")})
        if error or not payload.get("ok"):
            return _simple(payload, error, "")
        result = payload["result"]
        preview, preview_error = call("preview.get", {"max_side": 512})
        stats = (preview or {}).get("result", {}).get("stats", {}) if preview else {}
        lines = [f"曝光已设为 {result['value']:+.2f} EV"
                 + ("（原请求超出范围，已夹到边界）" if result.get("clamped") else "")]
        if stats:
            lines.append(
                f"画面统计：平均亮度 {stats.get('mean_luma')}，"
                f"高光削波 {stats.get('clipped_highlights_pct')}%，"
                f"暗部削波 {stats.get('clipped_shadows_pct')}%"
            )
            if stats.get("clipped_highlights_pct", 0) > 5:
                lines.append("提示：高光削波偏多，建议减小曝光值。")
            elif stats.get("mean_luma", 0.5) < 0.28:
                lines.append("提示：画面整体偏暗，可以考虑再提亮一点。")
        if preview_error:
            lines.append(f"（预览统计获取失败：{preview_error}）")
        return {"content": [_text("\n".join(lines))]}

    if name == "openlight_reset":
        adjustment_id = arguments.get("id") or "exposure"
        payload, error = call("adjustment.reset", {"id": adjustment_id})
        return _simple(payload, error, f"{adjustment_id} 已恢复默认值")

    if name == "openlight_undo":
        payload, error = call("history.undo")
        return _simple(payload, error, "已撤销")

    if name == "openlight_redo":
        payload, error = call("history.redo")
        return _simple(payload, error, "已重做")

    if name == "openlight_see_image":
        max_side = int(arguments.get("max_side") or 1024)
        data, error = fetch_preview(max_side)
        if error or not data:
            return {"isError": True,
                    "content": [_text(f"取图失败：{error or '没有数据'}。可能还没有打开照片。")]}
        state, _ = call("document.state")
        active = (state or {}).get("result", {}).get("document", {}).get("active_adjustments", {})
        label = ", ".join(f"{k}={v}" for k, v in active.items()) or "全部默认"
        return {
            "content": [
                {"type": "image", "data": base64.b64encode(data).decode("ascii"),
                 "mimeType": "image/png"},
                _text(f"当前调整：{label}（长边 {max_side}px 的预览）"),
            ]
        }

    if name == "openlight_export":
        params = {k: v for k, v in arguments.items() if v is not None}
        payload, error = call("export.image", params)
        if error or not payload.get("ok"):
            return _simple(payload, error, "")
        result = payload["result"]
        return {"content": [_text(
            f"已导出：{result['path']}\n"
            f"尺寸 {result['width']}x{result['height']}，格式 {result['format']}，"
            f"应用的调整：{json.dumps(result['adjustments'], ensure_ascii=False)}"
        )]}

    if name == "openlight_runs":
        payload, error = call("events.list", {
            "since": arguments.get("since", 0), "limit": arguments.get("limit", 20)
        })
        if error or not payload.get("ok"):
            return _simple(payload, error, "")
        lines = []
        for event in payload["result"]["events"]:
            lines.append(f"#{event['seq']} [{event['source']}] {event['type']} "
                         f"{json.dumps(event['data'], ensure_ascii=False)[:160]}")
        return {"content": [_text("\n".join(lines) or "没有新事件。")]}

    return {"isError": True, "content": [_text(f"未知工具：{name}")]}


def _adjustment_receipt(payload: dict | None, error: str | None) -> dict:
    """调整完顺手把画面统计带回去，agent 不用再多问一次。"""
    if error or not payload or not payload.get("ok"):
        return _simple(payload, error, "")
    result = payload["result"]
    value = result.get("value")
    text = f"{result['id']} 已设为 {json.dumps(value, ensure_ascii=False)}"
    if result.get("clamped"):
        text += "（原请求超出范围，已夹到边界）"
    preview, preview_error = call("preview.get", {"max_side": 512})
    stats = (preview or {}).get("result", {}).get("stats", {}) if preview else {}
    if stats:
        text += (f"\n画面统计：平均亮度 {stats.get('mean_luma')}，"
                 f"高光削波 {stats.get('clipped_highlights_pct')}%，"
                 f"暗部削波 {stats.get('clipped_shadows_pct')}%")
        text += "\n生效的调整级：" + "、".join(stats.get("applied") or [])
        if stats.get("clipped_highlights_pct", 0) > 5:
            text += "\n提示：高光削波偏多，考虑回退一点。"
    elif preview_error:
        text += f"\n（画面统计取不到：{preview_error}）"
    return {"content": [_text(text)]}


def _simple(payload: dict | None, error: str | None, success_text: str) -> dict:
    if error:
        return {"isError": True, "content": [_text(error)]}
    if not payload or not payload.get("ok"):
        detail = (payload or {}).get("error") or {}
        return {"isError": True, "content": [_text(
            f"失败：{detail.get('message', '未知原因')}\n提示：{detail.get('hint', '')}".strip()
        )]}
    result = json.dumps(payload.get("result"), ensure_ascii=False, default=str)
    return {"content": [_text((success_text + "\n" if success_text else "") + result)]}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------
def handle_request(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _ok(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"{APP_NAME.lower()}-photo-editor", "version": __version__},
            "instructions": (
                "openLight 是一个照片处理软件（42 个调整项：白平衡、基本、偏好、色调曲线、"
                "点曲线 RGB/R/G/B、HSL 混色器 8 色带 x 色相/饱和度/明亮度）。"
                "典型流程：openlight_schema 看有哪些参数 -> openlight_open_image 打开照片 -> "
                "openlight_see_image 看当前画面 -> openlight_adjust / openlight_set_adjustment "
                "调整 -> 再看图确认，直到满意 -> openlight_export 导出。"
                "每一步都会返回画面统计（平均亮度、高光/暗部削波比例），用它判断有没有调过头："
                "曝光单位是 EV（+1 就是亮度翻倍），色温/对比度一类是 -100~+100 的相对量，"
                "先小幅试（±10~±30）再迭代。"
            ),
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            return _ok(request_id, invoke_tool(name, arguments))
        except Exception as exc:
            return _ok(request_id, {"isError": True,
                                    "content": [_text(f"工具执行异常：{type(exc).__name__}: {exc}")]})
    if method in ("resources/list", "prompts/list"):
        return _ok(request_id, {"resources": []} if method.startswith("resources") else {"prompts": []})
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"不支持的方法：{method}"},
    }


def _ok(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    """stdio 主循环：一行一个 JSON-RPC 消息。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            continue
        response = handle_request(message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
