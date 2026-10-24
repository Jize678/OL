"""agent 用的命令行客户端。

它做的事和 GUI 完全一样——把命令发到同一个命令总线，只是没有界面。
命令行参数是给「会写 shell 的 agent」准备的糖，底层还是 POST /command。

用法示例：
    python -m openlight.cli schema
    python -m openlight.cli open "D:\\photo\\a.jpg"
    python -m openlight.cli exposure +0.8
    python -m openlight.cli set contrast 25
    python -m openlight.cli set hsl_blue_lum -30
    python -m openlight.cli curve curve_rgb '[[0,0],[0.5,0.6],[1,1]]'
    python -m openlight.cli preview --out look.png
    python -m openlight.cli export "D:\\out\\a.jpg"
    python -m openlight.cli undo
    python -m openlight.cli raw '{"cmd":"document.state"}'
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import APP_NAME, __version__
from .api.server import resolve_base_url as resolve_service_url

DEFAULT_PORT = 8765


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def resolve_base_url(args) -> str:
    """确定要连哪个服务；实在找不到就用默认地址，让报错信息更具体。"""
    port = getattr(args, "port", None) or DEFAULT_PORT
    return resolve_service_url(getattr(args, "url", None), port) or f"http://127.0.0.1:{port}"


def post_command(base_url: str, request: dict, timeout: float = 120.0) -> dict:
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        f"{base_url}/command",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(payload)
        except Exception:
            return {"ok": False, "error": {"code": "http_error", "message": payload}}
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": {
                "code": "not_connected",
                "message": f"连不上 {base_url}：{exc.reason}",
                "hint": "先启动软件：python run_gui.py（带界面）或 python run_api.py（无界面）。",
            },
        }


def _print_result(payload: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if payload.get("ok") else 1

    if not payload.get("ok"):
        error = payload.get("error") or {}
        print(f"× {error.get('message', '执行失败')}", file=sys.stderr)
        if error.get("hint"):
            print(f"  提示：{error['hint']}", file=sys.stderr)
        if error.get("code"):
            print(f"  错误码：{error['code']}", file=sys.stderr)
        return 1

    result = payload.get("result") or {}
    cmd = payload.get("cmd")
    if cmd == "app.schema":
        print(f"{APP_NAME} 可用命令：")
        for item in result.get("commands", []):
            print(f"  {item['cmd']:24s} {item['summary']}")
        specs = {spec["id"]: spec for spec in result.get("adjustments", [])}
        print(f"\n可调参数（{result.get('parameter_count', len(specs))} 项）：")
        for group in result.get("groups", []):
            print(f"  [{group['id']}]")
            for adjustment_id in group["adjustments"]:
                spec = specs[adjustment_id]
                if spec["kind"] == "curve":
                    print(f"    {spec['id']:24s} {spec['label']}  曲线（默认就是直线）")
                else:
                    print(f"    {spec['id']:24s} {spec['label']}  "
                          f"{spec['minimum']:g}~{spec['maximum']:g} {spec['unit']}  "
                          f"默认 {spec['default']:g}")
    elif cmd == "document.state" or "document" in result:
        document = result.get("document")
        if not document:
            print("当前没有打开照片。")
        else:
            print(f"{document['filename']}  {document['width']}x{document['height']}  "
                  f"{document['megapixels']}MP")
            active = document.get("active_adjustments") or {}
            print("当前调整：" + (", ".join(f"{k}={v}" for k, v in active.items()) or "全部默认"))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openlight", description=f"{APP_NAME} {__version__} 命令行 / agent 入口"
    )
    # 这些开关放在子命令前后都能用：argparse 的子解析器默认值会覆盖父解析器的值，
    # 所以在子解析器里用 SUPPRESS，只有真正传了才写入。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS,
                        help="服务地址，默认从 endpoint.json 自动发现")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="端口，默认 8765")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出完整 JSON（agent 建议开启）")
    parser.add_argument("--url", help="服务地址，默认从 endpoint.json 自动发现")
    parser.add_argument("--port", type=int, help="端口，默认 8765")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON（agent 建议开启）")

    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    add("schema", help="查看全部命令和可调参数")
    add("info", help="查看软件信息")
    add("state", help="查看当前照片状态")
    add("undo", help="撤销")
    add("redo", help="重做")

    open_parser = add("open", help="打开照片")
    open_parser.add_argument("path")

    add("close", help="关闭当前照片")

    exposure = add("exposure", help="设置曝光（EV）")
    exposure.add_argument("value", type=float)

    setter = add("set", help="设置任意调整项（曲线参数传 JSON 控制点）")
    setter.add_argument("id")
    setter.add_argument("value", help="数字，或曲线的控制点 JSON，如 '[[0,0],[1,1]]'")

    curve = add("curve", help="设置点曲线")
    curve.add_argument("id", nargs="?", default="curve_rgb",
                       help="curve_rgb / curve_red / curve_green / curve_blue")
    curve.add_argument("points", help="控制点 JSON，如 '[[0,0],[0.5,0.6],[1,1]]'")

    reset = add("reset", help="重置调整项")
    reset.add_argument("id", nargs="?", default="exposure")

    add("reset-all", help="全部重置")

    preview = add("preview", help="渲染预览图")
    preview.add_argument("--out", help="保存到文件；不填则打印 base64 长度")
    preview.add_argument("--max-side", type=int, default=1400)

    export = add("export", help="导出处理后的照片")
    export.add_argument("path", nargs="?")
    export.add_argument("--format")
    export.add_argument("--quality", type=int)
    export.add_argument("--max-side", type=int)
    export.add_argument("--overwrite", action="store_true")

    events = add("events", help="读取事件日志")
    events.add_argument("--since", type=int, default=0)
    events.add_argument("--limit", type=int, default=50)

    raw = add("raw", help="直接发一条 JSON 命令")
    raw.add_argument("json_command")

    return parser


def _parse_value(raw: str):
    """命令行上的取值：能当数字就当数字，否则按 JSON 解析（曲线控制点用得上）。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        return json.loads(raw)
    except Exception as exc:
        raise SystemExit(f"取值既不是数字也不是合法 JSON：{raw}（{exc}）")


def to_request(args) -> dict:
    command = args.command
    if command == "open":
        return {"cmd": "document.open", "params": {"path": args.path}}
    if command == "close":
        return {"cmd": "document.close"}
    if command == "exposure":
        return {"cmd": "adjustment.set", "params": {"id": "exposure", "value": args.value}}
    if command == "set":
        return {"cmd": "adjustment.set",
                "params": {"id": args.id, "value": _parse_value(args.value)}}
    if command == "curve":
        return {"cmd": "curve.set_points",
                "params": {"id": args.id, "points": _parse_value(args.points)}}
    if command == "reset":
        return {"cmd": "adjustment.reset", "params": {"id": args.id}}
    if command == "reset-all":
        return {"cmd": "adjustments.reset_all"}
    if command == "preview":
        params = {"max_side": args.max_side, "format": "png"}
        if args.out:
            params["path"] = args.out
            return {"cmd": "preview.save", "params": params}
        return {"cmd": "preview.get", "params": params}
    if command == "export":
        params: dict = {"overwrite": bool(args.overwrite)}
        if args.path:
            params["path"] = args.path
        if args.format:
            params["format"] = args.format
        if args.quality:
            params["quality"] = args.quality
        if args.max_side:
            params["max_side"] = args.max_side
        return {"cmd": "export.image", "params": params}
    if command == "events":
        return {"cmd": "events.list", "params": {"since": args.since, "limit": args.limit}}
    if command == "raw":
        try:
            return json.loads(args.json_command)
        except Exception as exc:
            raise SystemExit(f"JSON 解析失败：{exc}")
    return {"cmd": {"schema": "app.schema", "info": "app.info", "state": "document.state",
                    "undo": "history.undo", "redo": "history.redo"}[command]}


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)
    base_url = resolve_base_url(args)
    request = to_request(args)

    if args.command == "preview" and not args.out:
        # 预览走二进制端点，agent 拿到的是真图片文件，不用自己解 base64
        url = f"{base_url}/preview?max_side={args.max_side}"
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
        except Exception as exc:
            print(f"× 取预览失败：{exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"ok": True, "bytes": len(data),
                              "base64": base64.b64encode(data).decode("ascii")}))
        else:
            print(f"预览图 {len(data)} 字节（{base_url}/preview）")
            print("提示：加 --out 存文件，或直接访问上面这个 URL。")
        return 0

    payload = post_command(base_url, request)
    return _print_result(payload, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
