"""自动曝光演示：把「一句话 -> agent -> 调用软件 -> 完成修图」这条路走通。

它扮演的是 agent 的角色：打开照片 → 看画面统计 → 调整曝光 → 再看 → 直到满意 → 导出。
每一步都只通过 openLight 的接口（HTTP /command、/preview），
和真正的多模态 agent（通过 MCP 接进来）用的是同一条路。

    python tools/auto_exposure.py "D:\\photo\\a.jpg" --target 0.55
    python tools/auto_exposure.py "D:\\photo\\a.jpg" --target 0.45 --out "D:\\photo\\a_fixed.jpg"
    python tools/auto_exposure.py "D:\\photo\\a.jpg" --local     # 不开软件也能跑（进程内直连内核）
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from openlight.api.server import resolve_base_url  # noqa: E402


def srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


class HttpClient:
    """通过 HTTP 接口驱动正在运行的软件。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def command(self, cmd: str, **params) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/command",
            data=json.dumps({"cmd": cmd, "params": params}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read().decode("utf-8"))


class LocalClient:
    """进程内直连内核，不需要先打开软件界面。"""

    def __init__(self) -> None:
        from openlight.config import Settings
        from openlight.core.session import Session

        self.session = Session(Settings())

    def command(self, cmd: str, **params) -> dict:
        return self.session.execute({"cmd": cmd, "params": params, "source": "agent"})


def choose_client(args):
    if args.local:
        return LocalClient(), "进程内内核（--local）"
    found = resolve_base_url(args.url)
    if found:
        return HttpClient(found), found
    raise SystemExit(
        "没有找到正在运行的 openLight。\n"
        "先启动软件：python run_gui.py（带界面）或 python run_api.py（无界面）；\n"
        "或者加 --local 参数直接在进程内跑内核。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="自动根据画面亮度调整曝光")
    parser.add_argument("image", help="照片路径")
    parser.add_argument("--target", type=float, default=0.55,
                        help="目标平均亮度 0~1（默认 0.55，略亮于中灰）")
    parser.add_argument("--tolerance", type=float, default=0.02, help="收敛容差")
    parser.add_argument("--out", help="导出路径；不填则只调整不导出")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-clip", type=float, default=3.0,
                        help="允许的高光削波上限（百分比）")
    parser.add_argument("--url", help="指定服务地址")
    parser.add_argument("--local", action="store_true", help="不开软件，进程内直接跑")
    args = parser.parse_args()

    client, where = choose_client(args)
    print(f"连接到 {where}")

    opened = client.command("document.open", path=str(Path(args.image).expanduser()))
    if not opened.get("ok"):
        print("打开失败：", opened.get("error"))
        return 1
    document = opened["state"]["document"]
    print(f"打开成功：{document['filename']}  {document['width']}x{document['height']}")

    exposure = 0.0
    print(f"\n目标平均亮度 {args.target:.2f}，开始迭代：")
    print(f"{'步':>3} {'曝光 EV':>9} {'平均亮度':>10} {'高光削波%':>10} {'动作':<12}")

    for step in range(1, args.max_steps + 1):
        if step > 1:
            result = client.command("adjustment.set", id="exposure", value=round(exposure, 3))
            if not result.get("ok"):
                print("调整失败：", result.get("error"))
                return 1
            exposure = result["result"]["value"]

        preview = client.command("preview.get", max_side=512)
        if not preview.get("ok"):
            print("取预览失败：", preview.get("error"))
            return 1
        stats = preview["result"]["stats"]
        mean = stats["mean_luma"]
        clipped = stats["clipped_highlights_pct"]
        error = args.target - mean

        if abs(error) <= args.tolerance and clipped <= args.max_clip:
            print(f"{step:>3} {exposure:>+9.2f} {mean:>10.3f} {clipped:>10.2f} {'收敛，停止':<12}")
            break

        # 亮度误差换算成 EV：在线性空间里求需要的倍数，再取对数。
        current_linear = max(srgb_to_linear(min(max(mean, 0.001), 0.999)), 1e-4)
        target_linear = max(srgb_to_linear(min(max(args.target, 0.001), 0.999)), 1e-4)
        delta = math.log2(target_linear / current_linear)

        if clipped > args.max_clip:            # 高光快爆了，先往回退
            delta = min(delta, -0.2)
            action = "回退护高光"
        else:
            delta = max(-0.8, min(0.8, delta * 0.8))   # 阻尼 + 单步限幅，避免来回震荡
            action = "提亮" if delta > 0 else "压暗"
        exposure = round(max(-5.0, min(5.0, exposure + delta)), 3)
        print(f"{step:>3} {exposure:>+9.2f} {mean:>10.3f} {clipped:>10.2f} {action:<12}")
    else:
        print("达到最大步数，取当前结果。")

    if args.out:
        exported = client.command("export.image", path=str(Path(args.out).expanduser()),
                                  overwrite=True)
        if not exported.get("ok"):
            print("导出失败：", exported.get("error"))
            return 1
        info = exported["result"]
        print(f"\n已导出：{info['path']}（{info['width']}x{info['height']}，"
              f"曝光 {info['adjustments']['exposure']:+.2f} EV）")
    else:
        print(f"\n最终曝光 {exposure:+.2f} EV（加 --out 可以导出成品）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
