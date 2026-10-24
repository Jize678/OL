"""双击这个文件就能启动带界面的 openLight。

（.py 关联到 pythonw 时不会弹黑框；用 python run_gui.py 启动也能看到日志。）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openlight.ui.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
