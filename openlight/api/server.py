"""本地 HTTP + JSON API —— agent 操作这个软件的主要通道。

只用标准库实现（http.server），不引入 fastapi/uvicorn，保证装完就能跑。

约定
----
- 默认只绑定 127.0.0.1，不对局域网暴露。
- 所有命令统一走 `POST /command`，body 是 {"cmd": ..., "params": {...}}。
- `GET /schema` 是 agent 的自述文档入口。
- `GET /preview` 直接返回图片二进制，agent 可以直接看图（省掉 base64 解码）。
- 如果设置了环境变量 OPENLIGHT_TOKEN，则所有请求都要带 `X-OpenLight-Token` 头
  或 `?token=`，用于多用户环境下的本地防护。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import APP_NAME, __version__
from ..config import app_data_dir
from .gateway import CommandGateway

# 正在运行的服务实例（供 app.info 汇报地址）
_SERVICE: dict | None = None


def endpoint_file() -> Path:
    """把「服务在哪里」写到固定位置，agent 不用猜端口。"""
    return app_data_dir() / "endpoint.json"


def read_endpoint() -> dict | None:
    path = endpoint_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data or None


def _endpoint_alive(data: dict) -> bool:
    """端口记录指向的服务是不是真的还活着（用来判断记录是否过期）。"""
    if not data:
        return False
    try:
        host = str(data.get("host") or "127.0.0.1")
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def _write_endpoint(base_url: str, host: str, port: int) -> None:
    # 已经有别的实例在跑，就别抢它的位置（测试脚本、临时进程尤其不该覆盖）。
    existing = read_endpoint()
    if existing and existing.get("pid") != os.getpid() and _endpoint_alive(existing):
        if existing.get("port") != port:
            return
    try:
        endpoint_file().write_text(
            json.dumps(
                {
                    "base_url": base_url,
                    "host": host,
                    "port": port,
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "app": f"{APP_NAME} {__version__}",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _clear_endpoint() -> None:
    # 只清理自己写下的记录，不要误删别的实例的。
    try:
        existing = read_endpoint()
        if existing and existing.get("pid") not in (None, os.getpid()):
            return
        endpoint_file().unlink(missing_ok=True)
    except Exception:
        pass


def describe_service(session) -> dict:
    """给 agent 的服务自述：我该往哪儿发命令。"""
    if _SERVICE and _SERVICE.get("server") is not None:
        return {
            "transport": "http",
            "running": True,
            "base_url": _SERVICE["base_url"],
            "endpoints": _SERVICE["endpoints"],
            "auth": "token" if os.environ.get("OPENLIGHT_TOKEN") else "none (localhost only)",
        }
    return {
        "transport": "http",
        "running": False,
        "hint": "HTTP 服务未启动。用 `python run_gui.py` 或 `python run_api.py` 启动后即可远程调用。",
    }


DEFAULT_PORT = 8765


def resolve_base_url(explicit: str | None = None, default_port: int = DEFAULT_PORT) -> str | None:
    """找出正在运行的 openLight 在哪。顺序：显式地址 -> 端口记录 -> 默认端口。

    客户端（CLI / MCP / 脚本）都用它，保证「找不到服务」的判断是一致的。
    """
    if explicit:
        return explicit.rstrip("/")

    override = os.environ.get("OPENLIGHT_URL")
    if override:
        return override.rstrip("/")

    recorded = read_endpoint()
    if recorded and recorded.get("base_url") and _endpoint_alive(recorded):
        return str(recorded["base_url"]).rstrip("/")

    # 记录文件可能不存在（比如配置目录被沙箱挡住），退回到约定端口再试一次。
    fallback = {"host": "127.0.0.1", "port": default_port}
    if _endpoint_alive(fallback):
        return f"http://127.0.0.1:{default_port}"
    return None


def _endpoints() -> dict:
    return {
        "GET /": "人类可读的服务总览页",
        "GET /health": "存活探测",
        "GET /info": "软件能力与服务地址",
        "GET /schema": "全部命令与调整项的自述文档",
        "GET /state": "当前状态快照",
        "GET /commands": "命令目录（等同 /schema 的 commands 字段）",
        "GET /events?since=0&limit=200": "事件日志，用于与人的操作保持同步",
        "GET /preview?max_side=1400&format=png": "直接返回预览图二进制",
        "GET /preview?base64=1": "预览图的 base64 JSON（含画面统计）",
        "POST /command": "执行一条命令，body = {\"cmd\": ..., \"params\": {...}}",
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # 静音，避免刷屏
        if os.environ.get("OPENLIGHT_HTTP_LOG"):
            super().log_message(fmt, *args)

    @property
    def gateway(self) -> CommandGateway:
        return self.server.gateway  # type: ignore[attr-defined]

    @property
    def session(self):
        return self.server.gateway.session  # type: ignore[attr-defined]

    def _authorized(self, query: dict) -> bool:
        token = os.environ.get("OPENLIGHT_TOKEN")
        if not token:
            return True
        header = self.headers.get("X-OpenLight-Token")
        return header == token or (query.get("token", [None])[0] == token)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OpenLight-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if not self._authorized(query):
            self._send_json({"ok": False, "error": {"code": "unauthorized",
                                                    "message": "缺少或错误的 token。"}}, 401)
            return

        if route == "/":
            self._send_text(_landing_page(self.server), content_type="text/html; charset=utf-8")
            return
        if route == "/health":
            self._send_json({"ok": True, "service": APP_NAME, "version": __version__})
            return
        if route in ("/info", "/schema", "/state", "/commands"):
            cmd = {"/info": "app.info", "/schema": "app.schema",
                   "/state": "document.state", "/commands": "app.schema"}[route]
            response = self.gateway.execute({"cmd": cmd, "params": {}}, source="agent")
            if route == "/commands" and response.get("ok"):
                response = {"ok": True, "commands": response["result"]["commands"]}
            self._send_json(response, 200 if response.get("ok") else 400)
            return
        if route == "/events":
            params = {
                "since": int(query.get("since", ["0"])[0] or 0),
                "limit": int(query.get("limit", ["200"])[0] or 200),
            }
            response = self.gateway.execute({"cmd": "events.list", "params": params},
                                            source="agent")
            self._send_json(response, 200 if response.get("ok") else 400)
            return
        if route == "/preview":
            self._handle_preview(query)
            return
        self._send_json({"ok": False, "error": {"code": "not_found",
                                                "message": f"没有这个端点：{parsed.path}",
                                                "hint": "GET / 可以看到全部端点。"}}, 404)

    def _handle_preview(self, query: dict) -> None:
        max_side = int(query.get("max_side", [self.session.settings.preview_max_side])[0])
        fmt = (query.get("format", ["png"])[0] or "png").lower()
        wants_base64 = query.get("base64", ["0"])[0] in ("1", "true", "yes")

        if wants_base64:
            response = self.gateway.execute(
                {"cmd": "preview.get", "params": {"max_side": max_side, "format": fmt}},
                source="agent",
            )
            self._send_json(response, 200 if response.get("ok") else 400)
            return

        try:
            data = self.gateway.invoker.call(
                lambda: self.session.preview_png(max_side)
            )
        except Exception as exc:  # 没打开照片等
            self._send_json({"ok": False, "error": {"code": "preview_failed",
                                                    "message": str(exc),
                                                    "hint": "先用 document.open 打开一张照片。"}},
                            400)
            return
        content_type = "image/jpeg" if fmt in ("jpg", "jpeg") else "image/png"
        if content_type == "image/jpeg":
            from io import BytesIO

            result = self.gateway.invoker.call(
                lambda: self.session._render_preview(max_side)  # noqa: SLF001
            )
            buffer = BytesIO()
            result.image.save(buffer, format="JPEG", quality=90)
            data = buffer.getvalue()
        self._send_bytes(data, content_type)

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._send_json({"ok": False, "error": {"code": "unauthorized",
                                                    "message": "缺少或错误的 token。"}}, 401)
            return
        if parsed.path.rstrip("/") not in ("/command", "/cmd", "/rpc"):
            self._send_json({"ok": False, "error": {"code": "not_found",
                                                    "message": f"没有这个端点：{parsed.path}"}}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            self._send_json({"ok": False, "error": {"code": "bad_request",
                                                    "message": "请求体为空。"}}, 400)
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._send_json({"ok": False, "error": {"code": "bad_json",
                                                    "message": f"JSON 解析失败：{exc}"}}, 400)
            return
        if isinstance(request, list):
            responses = [self.gateway.execute(item, source="agent") for item in request]
            self._send_json({"ok": all(r.get("ok") for r in responses),
                             "results": responses})
            return

        response = self.gateway.execute(request, source="agent")
        self._send_json(response, 200 if response.get("ok") else 400)


def _landing_page(server) -> str:
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    rows = "".join(
        f"<tr><td><code>{name}</code></td><td>{desc}</td></tr>"
        for name, desc in _endpoints().items()
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{APP_NAME} agent API</title>
<style>
 body{{background:#14161a;color:#e8eaed;font:14px/1.6 "Segoe UI",system-ui,sans-serif;margin:40px}}
 code{{background:#1e2127;padding:2px 6px;border-radius:4px;color:#8ab4f8}}
 table{{border-collapse:collapse;margin-top:12px}} td{{padding:6px 14px;border-bottom:1px solid #2a2e36}}
 h1{{font-weight:600}} .tag{{color:#9aa0a6}}
</style></head><body>
<h1>{APP_NAME} <span class="tag">v{__version__}</span></h1>
<p>这是 openLight 的本地 agent 接口，人和 agent 用的都是同一套命令。</p>
<p>自述文档：<code>GET {base_url}/schema</code></p>
<table>{rows}</table>
<p>示例：<code>curl -s {base_url}/command -H "Content-Type: application/json" -d "{{\\"cmd\\":\\"app.schema\\"}}"</code></p>
</body></html>"""


class ApiServer:
    """把 HTTP 服务跑在后台线程里，界面线程继续画图。"""

    def __init__(self, session, host: str = "127.0.0.1", port: int = 8765,
                 gateway: CommandGateway | None = None) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.gateway = gateway or CommandGateway(session)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def base_url(self) -> str:
        if self._httpd is not None:
            host, port = self._httpd.server_address[:2]
            return f"http://{host}:{port}"
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        global _SERVICE
        if self.running:
            return self.base_url

        # Windows 上 SO_REUSEADDR 允许两个进程绑同一个端口——两个服务会互相抢连接。
        # 所以开服前先探一下：这个端口上已经有人监听就让开。
        if self.port and _endpoint_alive({"host": self.host, "port": self.port}):
            self.port = find_free_port(self.host, self.port + 1)

        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError:
            # 端口被占用（比如开了两个窗口）时自动换一个
            self.port = find_free_port(self.host, self.port + 1)
            httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.gateway = self.gateway  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="openlight-api",
                                        daemon=True)
        self._thread.start()
        _SERVICE = {
            "server": httpd,
            "base_url": self.base_url,
            "endpoints": _endpoints(),
        }
        host, port = httpd.server_address[:2]
        _write_endpoint(self.base_url, host, port)
        return self.base_url

    def stop(self) -> None:
        global _SERVICE
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._httpd = None
        self._thread = None
        _SERVICE = None
        _clear_endpoint()


def find_free_port(host: str = "127.0.0.1", preferred: int = 8765) -> int:
    """端口被占用时自动找下一个可用端口。

    注意这里**不能**设置 SO_REUSEADDR：Windows 下它会让探测在「端口已被占用」
    时依然成功，结果是两个进程绑同一个端口。
    """
    for candidate in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("找不到可用端口。")
