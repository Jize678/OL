"""结构化错误。

agent 拿到的错误必须能被程序化处理：有稳定的 code、给人看的信息、以及
「下一步该怎么办」的提示。不要用裸的 Exception 跨越接口边界。
"""

from __future__ import annotations


class OpenLightError(Exception):
    """所有可预期的业务错误都用它，会被 API 层序列化成 JSON。"""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def bad_request(message: str, hint: str | None = None) -> OpenLightError:
    return OpenLightError("bad_request", message, hint)


def unknown_command(cmd: str, hint: str | None = None) -> OpenLightError:
    return OpenLightError(
        "unknown_command",
        f"未知命令：{cmd}",
        hint or "调用 app.schema 获取全部可用命令及其参数说明。",
    )


def no_document() -> OpenLightError:
    return OpenLightError(
        "no_document",
        "当前没有打开的照片。",
        "先执行 document.open（参数 path 指向一张本地图片）。",
    )


def not_found(message: str, hint: str | None = None) -> OpenLightError:
    return OpenLightError("not_found", message, hint)


def out_of_range(name: str, value: float, low: float, high: float) -> OpenLightError:
    return OpenLightError(
        "out_of_range",
        f"参数 {name}={value} 超出允许范围 [{low}, {high}]。",
        f"把 {name} 调整到 {low} ~ {high} 之间再试。",
    )
