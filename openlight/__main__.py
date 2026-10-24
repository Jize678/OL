"""python -m openlight  ->  启动图形界面。python -m openlight serve -> 无界面 API 服务。"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("serve", "api", "headless"):
        from run_api import main as serve_main

        return serve_main(argv[1:])
    if argv and argv[0] in ("cli", "cmd"):
        from .cli import main as cli_main

        return cli_main(argv[1:])

    from .ui.app import main as gui_main

    return gui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
