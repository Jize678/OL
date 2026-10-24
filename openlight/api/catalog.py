"""命令目录（机器可读的自述文档）。

agent 的第一件事应该是调用 `app.schema`，拿到：
- 有哪些命令；
- 每条命令接受什么参数、参数类型、是否必填；
- 参数范围和默认值；
- 一个可以直接照抄的例子。

新增命令后必须在这里登记，否则 agent「看不见」它。
"""

from __future__ import annotations


def _cmd(
    name: str,
    summary: str,
    *,
    params: dict | None = None,
    example: dict | None = None,
    mutates: bool = False,
    returns: str = "",
    aliases: list[str] | None = None,
) -> dict:
    return {
        "cmd": name,
        "summary": summary,
        "mutates_state": mutates,
        "params": params or {},
        "example": example or {"cmd": name, "params": {}},
        "returns": returns,
        "aliases": aliases or [],
    }


def command_catalog() -> list[dict]:
    return [
        _cmd(
            "app.info",
            "查询软件版本、能力清单和当前服务地址。第一次连接时先调它。",
            returns="name/version/capabilities/interface",
        ),
        _cmd(
            "app.schema",
            "返回全部可用命令、参数说明、调整项范围和分组。agent 的自述文档入口。",
            returns="commands[] / adjustments[] / groups[] / parameter_count",
            aliases=["schema.get"],
        ),
        _cmd("app.ping", "连通性探测。", returns="{pong: true}"),
        _cmd(
            "document.open",
            "打开一张本地照片，成为当前编辑对象。会清空历史记录。",
            params={
                "path": {"type": "string", "required": True,
                         "description": "图片绝对路径，支持 jpg/png/tif/webp/bmp/gif。"},
                "adjustments": {"type": "object", "required": False,
                                "description": "可选，打开时直接套用的调整，如 {\"exposure\": 0.5}。"},
            },
            example={"cmd": "document.open", "params": {"path": "D:\\photo\\a.jpg"}},
            mutates=True,
            returns="document",
        ),
        _cmd(
            "document.close",
            "关闭当前照片。",
            mutates=True,
            returns="{closed: true}",
        ),
        _cmd(
            "document.state",
            "查看当前照片信息：尺寸、EXIF、当前调整值、偏离默认值的项。",
            returns="document（未打开任何照片时 document 为 null）",
        ),
        _cmd(
            "adjustment.set",
            "设置某一个调整项的值。超出范围会被自动夹到边界，并在返回里标记 clamped=true。",
            params={
                "id": {"type": "string", "required": True,
                       "description": "调整项 id，见 app.schema 的 adjustments 字段。"
                                      "共 42 个，分 6 组：白平衡 / 基本 / 偏好 / 色调曲线 / "
                                      "曲线 / HSL 混色器。"},
                "value": {"type": ["number", "array"], "required": True,
                          "description": "标量参数传数字（exposure 单位 EV，范围 -5 ~ +5；"
                                         "其余标量基本都是 -100 ~ +100）；"
                                         "曲线参数传控制点，如 [[0,0],[0.5,0.6],[1,1]]。"},
            },
            example={"cmd": "adjustment.set", "params": {"id": "exposure", "value": 0.8}},
            mutates=True,
            returns="id / value / clamped / parameters",
        ),
        _cmd(
            "curve.set_points",
            "设置某条点曲线（curve_rgb / curve_red / curve_green / curve_blue）的控制点。"
            "和 adjustment.set 等价，但只接受曲线参数，意图更清楚。",
            params={
                "id": {"type": "string", "required": False, "default": "curve_rgb",
                       "enum": ["curve_rgb", "curve_red", "curve_green", "curve_blue"],
                       "description": "要改的曲线；省略时改 RGB 综合曲线。"},
                "points": {"type": "array", "required": True,
                           "description": "控制点列表 [[x, y], ...]，x 是输入亮度、y 是输出亮度，"
                                          "两者都在 0~1，x 必须递增，至少 2 个点。"
                                          "默认 [[0,0],[1,1]] 是 45 度直线（不改变画面）。"
                                          "插值用单调三次样条，永远不会过冲。"},
            },
            example={"cmd": "curve.set_points",
                     "params": {"id": "curve_rgb",
                                "points": [[0, 0], [0.25, 0.18], [0.75, 0.82], [1, 1]]}},
            mutates=True,
            returns="id / points / parameters",
        ),
        _cmd(
            "adjustment.reset",
            "把某一个调整项恢复默认值（标量回到 0，曲线回到 45 度直线）。",
            params={"id": {"type": "string", "required": True}},
            example={"cmd": "adjustment.reset", "params": {"id": "exposure"}},
            mutates=True,
        ),
        _cmd(
            "adjustments.set_batch",
            "一次设置多个调整项，只占用一个历史步骤。做整体风格时用它，比逐条发命令少很多次往返。",
            params={"adjustments": {"type": "object", "required": True,
                                    "description": "如 {\"exposure\": 0.6, \"contrast\": 25, "
                                                   "\"curve_rgb\": [[0,0],[0.5,0.55],[1,1]]}"}},
            example={"cmd": "adjustments.set_batch", "params": {"adjustments": {"exposure": 0.6}}},
            mutates=True,
        ),
        _cmd(
            "adjustments.reset_all",
            "把全部调整项恢复默认值（回到原图）。",
            mutates=True,
        ),
        _cmd("history.undo", "撤销上一步（人或 agent 的操作都能撤）。", mutates=True),
        _cmd("history.redo", "重做下一步。", mutates=True),
        _cmd("history.state", "查看历史栈：能否撤销/重做、每一步的标签和来源。"),
        _cmd(
            "preview.get",
            "渲染当前参数的预览图，返回 base64 PNG/JPEG 和画面统计（亮度均值、高光/暗部削波比例、"
            "实际生效的处理级 applied）。agent 用它做「调整 -> 看图 -> 再调整」的闭环。",
            params={
                "max_side": {"type": "integer", "required": False, "default": 1400,
                             "description": "预览图长边像素，越小越快。"},
                "format": {"type": "string", "required": False, "default": "png",
                           "enum": ["png", "jpeg"]},
            },
            returns="data_base64 / stats",
        ),
        _cmd(
            "preview.save",
            "把预览图直接存成 PNG 文件。比 base64 更适合 agent 反复查看。",
            params={
                "path": {"type": "string", "required": False,
                         "description": "省略时存到原图同目录的 <名字>.preview.png。"},
                "max_side": {"type": "integer", "required": False},
            },
            example={"cmd": "preview.save", "params": {"path": "D:\\tmp\\look.png"}},
            returns="path / width / height / stats",
        ),
        _cmd(
            "export.image",
            "按当前参数渲染全分辨率并导出到文件。默认不覆盖已存在的文件，也绝不允许覆盖原图。",
            params={
                "path": {"type": "string", "required": False,
                         "description": "输出路径；省略时写成 <原名>_openlight.<后缀>。"},
                "format": {"type": "string", "required": False,
                           "description": "jpg/png/tif/webp/bmp，默认跟随输出文件后缀。"},
                "quality": {"type": "integer", "required": False, "description": "JPEG/WEBP 质量 1-100。"},
                "max_side": {"type": "integer", "required": False, "description": "限制输出长边。"},
                "overwrite": {"type": "boolean", "required": False, "default": False},
            },
            example={"cmd": "export.image", "params": {"path": "D:\\out\\a_edited.jpg"}},
            returns="path / width / height / stats",
        ),
        _cmd(
            "recipe.save",
            "把当前调整写成 sidecar 文件（<原文件名>.openlight.json），下次打开自动套用。",
            returns="path / recipe",
        ),
        _cmd("recipe.reload", "从 sidecar 文件重新载入调整。"),
        _cmd(
            "events.list",
            "读取事件日志。人手动在界面上的操作也会出现在这里，agent 用它保持同步。",
            params={
                "since": {"type": "integer", "required": False, "default": 0,
                          "description": "只返回 seq 大于该值的事件。"},
                "limit": {"type": "integer", "required": False, "default": 200},
            },
            returns="events[] / last_seq",
        ),
    ]


def command_index() -> dict[str, dict]:
    index = {}
    for item in command_catalog():
        index[item["cmd"]] = item
        for alias in item["aliases"]:
            index[alias] = item
    return index
