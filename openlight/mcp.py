"""`python -m openlight.mcp` 的入口。"""

from .api.mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main())
