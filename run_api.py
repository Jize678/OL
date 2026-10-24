"""无界面模式：只跑 agent 接口服务。

适合放在后台，让 agent 独立驱动整个软件（此时没有窗口，但命令、渲染、
导出、历史全部可用）。

    python run_api.py                 # 一直跑
    python run_api.py --port 9000     # 换端口
    python run_api.py --open D:\\a.jpg --exposure 0.6 --export D:\\out\\a.jpg
    python run_api.py --open D:\\a.jpg --set contrast=25 --set temperature=-15 \\
                      --set curve_rgb=[[0,0],[0.5,0.6],[1,1]] --export D:\\out.jpg \\
                      --exit-after-export
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from openlight.api.gateway import CommandGateway  # noqa: E402
from openlight.api.server import ApiServer  # noqa: E402
from openlight.config import Settings  # noqa: E402
from openlight.core.session import Session  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="openLight 无界面服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--open", dest="open_path", help="启动后立刻打开这张照片")
    parser.add_argument("--exposure", type=float, help="打开后立刻设置的曝光值")
    parser.add_argument("--set", action="append", default=[], metavar="ID=VALUE",
                        help="打开后设置的调整项，可重复。值可以是数字，"
                             "也可以是曲线的控制点 JSON，如 --set curve_rgb=[[0,0],[0.5,0.6],[1,1]]")
    parser.add_argument("--export", dest="export_path", help="导出到指定路径后继续运行")
    parser.add_argument("--exit-after-export", action="store_true",
                        help="导出完成后直接退出（一次性批处理）")
    return parser


def parse_assignments(items: list[str]) -> dict:
    """把 --set id=value 解析成参数字典。值先按 JSON 解析，再退回数字。"""
    assignments: dict = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key.strip():
            raise SystemExit(f"--set 需要 ID=VALUE 形式，收到：{item}")
        raw = raw.strip()
        try:
            value = json.loads(raw)
        except Exception:
            try:
                value = float(raw)
            except ValueError:
                raise SystemExit(f"--set 的值既不是数字也不是合法 JSON：{raw}")
        assignments[key.strip()] = value
    return assignments


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    if args.host:
        settings.api_host = args.host
    if args.port:
        settings.api_port = args.port

    session = Session(settings)
    gateway = CommandGateway(session)      # 无界面时不需要跨线程编排
    api = ApiServer(session, settings.api_host, settings.api_port, gateway=gateway)
    base_url = api.start()

    print(f"openLight agent 服务已启动：{base_url}")
    print(f"自述文档：{base_url}/schema")
    print("命令示例：curl -s %s/command -H \"Content-Type: application/json\" "
          "-d \"{\\\"cmd\\\":\\\"document.state\\\"}\"" % base_url)
    print("按 Ctrl+C 退出。\n")

    def run(cmd: str, **params):
        response = session.execute({"cmd": cmd, "params": params, "source": "agent"})
        ok = "✓" if response.get("ok") else "×"
        print(f"{ok} {cmd} {json.dumps(params, ensure_ascii=False)}"
              + ("" if response.get("ok") else f" -> {response.get('error')}"))
        return response

    if args.open_path:
        run("document.open", path=args.open_path)
    if args.exposure is not None:
        run("adjustment.set", id="exposure", value=args.exposure)
    if args.set:
        run("adjustments.set_batch", adjustments=parse_assignments(args.set))
    if args.export_path:
        result = run("export.image", path=args.export_path, overwrite=True)
        if args.exit_after_export:
            api.stop()
            return 0 if result.get("ok") else 1

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在退出……")
    finally:
        api.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
