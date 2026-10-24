# 给后续 agent 的项目约定

本项目是「agent 友好型照片处理软件」。改动代码时必须遵守下面的约定，否则会破坏核心设计。

## 铁律

1. **任何状态变更都必须走 `openlight.core.session.Session.execute(command)`。**
   GUI 代码里不允许直接给 `PhotoDocument.params` 赋值。新增功能时，先加命令，再在 GUI 里调用命令。
2. **新增调整项 = 在 `openlight/core/adjustments.py` 注册 + 在 `openlight/core/engine.py` 实现像素运算。**
   调整项的 `min/max/default/unit/description` 会通过 `app.schema` 自动暴露给 agent。
3. **每个新命令都要在 `openlight/api/catalog.py` 里登记**，包含参数说明和示例。
   agent 看不到的命令等于不存在。
4. **命令必须返回结构化结果**，失败时抛 `OpenLightError(code, message, hint)`。
5. **不要引入重量级框架**：API 层用标准库 `http.server`，避免安装 fastapi/uvicorn。
6. 涉及图像像素的操作优先用 numpy 向量化，大图按行分块处理，避免内存爆掉。
7. **数学与编排分开**：颜色空间、曲线插值这类纯函数放 `core/color.py` / `core/curves.py`，
   `engine.py` 只负责「按什么顺序调用它们」。这样曲线和 HSL 都能单独测试。
8. **界面不许硬编码参数行**：侧栏按 `registry.groups()` / `registry.by_group()` 生成。
   加参数时界面什么都不用改，它自己会多出一行滑块。
   曲线这类非标量控件走 `ui/curve_editor.py` + 现成的 preview/committed 信号模式：
   拖动中只发预览、松手才发命令。

## 自检清单

改完代码后至少跑一遍：

```powershell
.\.venv\Scripts\python.exe tools\selfcheck.py   # 内核
.\.venv\Scripts\python.exe tools\apitest.py     # HTTP API / MCP / CLI
.\.venv\Scripts\python.exe tools\uitest.py      # 界面（离屏）
```

它们会用纯内核（不开界面）跑一遍「打开图片 -> 逐个调整 42 个参数 -> 撤销 -> 导出」、
校验 schema 完整性（参数数量、分组、示例、agent_hint）、以及界面与 agent 命令的同步。
`selfcheck.py` 最后还会打印几种典型参数组合的预览耗时，加算子时注意别让拖动变得卡顿
（目标：长边 1400 的预览，常用参数组合在 150ms 以内）。

## 关于「找不到服务」

客户端统一用 `openlight.api.server.resolve_base_url()` 定位正在运行的实例：
显式地址 -> `%APPDATA%\openLight\endpoint.json` -> 约定端口 8765。
不要在各处自己拼端口。
