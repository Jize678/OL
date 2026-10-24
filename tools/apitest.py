"""接口层集成测试：HTTP API、MCP、CLI 三条路都要能跑通。

    python tools/apitest.py
"""

from __future__ import annotations

import json
import io
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from openlight.api.gateway import CommandGateway  # noqa: E402
from openlight.api.mcp_server import handle_request, invoke_tool, tool_definitions  # noqa: E402
from openlight.api.server import ApiServer  # noqa: E402
from openlight.cli import build_parser, to_request  # noqa: E402
from openlight.config import Settings  # noqa: E402
from openlight.core.session import Session  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    RESULTS.append((bool(condition), label))
    print(("  [ok]   " if condition else "  [FAIL] ") + label)


def http_json(url: str, payload: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="openlight-apitest-"))
    source = workdir / "shot.jpg"
    ramp = np.tile(np.linspace(10, 250, 640, dtype=np.uint8), (480, 1))
    Image.fromarray(np.dstack([ramp, ramp, ramp]), "RGB").save(source, quality=95)

    settings = Settings()
    settings.api_port = 0            # 交给系统分配，避免和正在运行的实例撞车
    settings.preview_max_side = 800
    session = Session(settings)
    gateway = CommandGateway(session)
    api = ApiServer(session, "127.0.0.1", 0, gateway=gateway)
    base = api.start()
    print(f"测试服务：{base}\n")

    try:
        print("1) HTTP 端点")
        health = http_json(f"{base}/health")
        check(health.get("ok") is True, "GET /health")
        schema = http_json(f"{base}/schema")
        check(schema["ok"] and len(schema["result"]["commands"]) > 10,
              f"GET /schema 返回 {len(schema['result']['commands'])} 条命令")
        check(schema["result"]["parameter_count"] == 42
              and len(schema["result"]["groups"]) == 6,
              f"schema 自述 {schema['result']['parameter_count']} 个参数 / "
              f"{len(schema['result']['groups'])} 组")
        commands = http_json(f"{base}/commands")
        check(commands["ok"] and commands["commands"], "GET /commands")
        info = http_json(f"{base}/info")
        check(info["ok"] and info["result"]["interface"]["running"] is True,
              "GET /info 里能看到服务地址（agent 靠它自我定位）")

        print("\n2) POST /command")
        opened = http_json(f"{base}/command",
                           {"cmd": "document.open", "params": {"path": str(source)}},
                           method="POST")
        check(opened["ok"], "通过 HTTP 打开照片")
        check(opened["state"]["document"]["filename"] == "shot.jpg",
              "返回里带执行后的状态快照")
        set_result = http_json(f"{base}/command",
                               {"cmd": "adjustment.set",
                                "params": {"id": "exposure", "value": 0.7}}, method="POST")
        check(set_result["ok"] and set_result["result"]["value"] == 0.7, "通过 HTTP 调曝光")
        contrast = http_json(f"{base}/command",
                             {"cmd": "adjustment.set",
                              "params": {"id": "contrast", "value": 35}}, method="POST")
        check(contrast["ok"] and contrast["result"]["value"] == 35, "通过 HTTP 调对比度")
        curve = http_json(f"{base}/command",
                          {"cmd": "curve.set_points",
                           "params": {"id": "curve_rgb",
                                      "points": [[0, 0], [0.5, 0.62], [1, 1]]}},
                          method="POST")
        check(curve["ok"] and len(curve["result"]["points"]) == 3,
              "通过 HTTP 设置曲线控制点")
        batch = http_json(f"{base}/command",
                          {"cmd": "adjustments.set_batch",
                           "params": {"adjustments": {"temperature": -20, "vibrance": 25,
                                                      "hsl_blue_lum": -30}}},
                          method="POST")
        check(batch["ok"] and batch["result"]["applied"]["hsl_blue_lum"] == -30,
              "通过 HTTP 一次设置多个参数")

        print("\n3) 预览二进制端点（agent 直接看图）")
        with urllib.request.urlopen(f"{base}/preview?max_side=320", timeout=30) as response:
            content_type = response.headers.get("Content-Type")
            data = response.read()
        check(content_type == "image/png", f"Content-Type 正确（{content_type}）")
        with Image.open(io.BytesIO(data)) as decoded:
            check(decoded.format == "PNG" and max(decoded.size) == 320,
                  f"返回的是可解码的 PNG（{decoded.size[0]}x{decoded.size[1]}）")
        preview_json = http_json(f"{base}/preview?max_side=256&base64=1")
        check(preview_json["ok"] and preview_json["result"]["stats"]["gain"] > 1.5,
              "base64 模式返回画面统计")

        print("\n4) 事件流（人机同步的基础）")
        events = http_json(f"{base}/events?since=0")
        types = {e["type"] for e in events["result"]["events"]}
        check({"document.opened", "adjustment.changed"} <= types, "事件日志可读")
        check(all(e["source"] == "agent" for e in events["result"]["events"]),
              "HTTP 来的操作被标记为 agent")

        print("\n5) 快速连发命令（agent 常见行为）")
        batch = http_json(f"{base}/command", [
            {"cmd": "adjustment.set", "params": {"id": "exposure", "value": 0.2}},
            {"cmd": "adjustment.set", "params": {"id": "exposure", "value": 0.9}},
            {"cmd": "history.undo"},
        ], method="POST")
        check(batch["ok"] and len(batch["results"]) == 3, "支持一次提交多条命令")
        check(batch["results"][-1]["result"]["parameters"]["exposure"] == 0.2,
              "批量命令按顺序生效")

        print("\n6) MCP 协议")
        init = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2024-11-05"}})
        check(init["result"]["serverInfo"]["name"].startswith("openlight"),
              "initialize 握手成功")
        tools = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in tools["result"]["tools"]]
        check(len(names) >= 13, f"tools/list 返回 {len(names)} 个工具：{', '.join(names[:3])}…")
        check({"openlight_schema", "openlight_set_adjustment", "openlight_set_curve",
               "openlight_adjust"} <= set(names),
              "MCP 暴露了通用调整 / 曲线 / 批量 / 自述文档四个工具")
        check(all("inputSchema" in t for t in tools["result"]["tools"]),
              "每个工具都带 JSON Schema")
        set_via_mcp = handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "openlight_set_exposure", "arguments": {"value": 1.1}},
        })
        text = set_via_mcp["result"]["content"][0]["text"]
        check("1.10" in text and "画面统计" in text, "MCP 调曝光并返回画面统计")
        image_result = handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "openlight_see_image", "arguments": {"max_side": 256}},
        })
        blocks = image_result["result"]["content"]
        check(any(b.get("type") == "image" for b in blocks),
              "MCP 能直接把画面喂给多模态 agent（image content block）")
        schema_via_mcp = handle_request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "openlight_schema", "arguments": {"group": "HSL 混色器"}},
        })
        schema_text = schema_via_mcp["result"]["content"][0]["text"]
        check("hsl_blue_lum" in schema_text and "色带" not in schema_text.split("\n")[0],
              "MCP 自述文档能按组返回参数说明")
        adjust_via_mcp = handle_request({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "openlight_adjust", "arguments": {
                "adjustments": {"shadows": 30, "highlights": -25}}},
        })
        adjust_text = adjust_via_mcp["result"]["content"][0]["text"]
        check("shadows" in adjust_text and "已一次设置" in adjust_text,
              "MCP 批量调整返回结构化回执")
        curve_via_mcp = handle_request({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "openlight_set_curve", "arguments": {
                "id": "curve_blue", "points": [[0, 0.05], [1, 0.95]]}},
        })
        check("curve_blue" in curve_via_mcp["result"]["content"][0]["text"],
              "MCP 能设置曲线")
        unknown = invoke_tool("no_such_tool", {})
        check(unknown.get("isError"), "未知工具返回 isError")

        print("\n7) CLI 参数映射")
        parser = build_parser()
        mapping = [
            (["open", "D:\\a.jpg"], "document.open"),
            (["exposure", "0.5"], "adjustment.set"),
            (["set", "hsl_blue_lum", "-30"], "adjustment.set"),
            (["curve", "curve_rgb", "[[0,0],[0.5,0.6],[1,1]]"], "curve.set_points"),
            (["undo"], "history.undo"),
            (["export", "D:\\out.jpg", "--quality", "90"], "export.image"),
            (["preview", "--out", "p.png"], "preview.save"),
            (["raw", '{"cmd":"document.state"}'], "document.state"),
        ]
        for argv, expected in mapping:
            request = to_request(parser.parse_args(argv))
            check(request["cmd"] == expected, f"cli {' '.join(argv[:2])} -> {expected}")
        export_request = to_request(parser.parse_args(
            ["export", "D:\\out.jpg", "--quality", "90"]))
        check(export_request["params"]["quality"] == 90, "CLI 正确透传参数")
        curve_request = to_request(parser.parse_args(
            ["curve", "curve_rgb", "[[0,0],[0.5,0.6],[1,1]]"]))
        check(curve_request["params"]["points"] == [[0, 0], [0.5, 0.6], [1, 1]],
              "CLI 把曲线参数解析成控制点数组")
        scalar_request = to_request(parser.parse_args(["set", "contrast", "25"]))
        check(scalar_request["params"]["value"] == 25.0, "CLI 的 set 仍然接受纯数字")

        print("\n8) 服务自述（agent 冷启动的第一步）")
        info = http_json(f"{base}/info")
        interface = info["result"]["interface"]
        check(interface["transport"] == "http" and "POST /command" in interface["endpoints"],
              "info 里给出可用的端点清单")
    finally:
        api.stop()

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
