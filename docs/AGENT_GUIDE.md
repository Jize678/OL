# openLight 的 agent 接口

这篇文档是写给「要驱动这个软件的 agent」看的。人不用读它。

## 一句话总结

先问 `app.schema`，它会告诉你这个软件会什么；然后用 `POST /command`
把命令发进去。你在命令行敲的、和软件界面上点出来的，是同一条命令。

## 连接

软件启动后会在 `%APPDATA%\openLight\endpoint.json` 写下自己的地址，
所以不需要猜端口：

```json
{ "base_url": "http://127.0.0.1:8765", "port": 8765, "pid": 12345 }
```

命令行客户端会自动读这个文件：

```powershell
python -m openlight.cli state
```

## 三种调用方式

| 方式 | 适用场景 |
| --- | --- |
| HTTP `POST /command` | 通用，任何语言都能调 |
| `python -m openlight.cli <子命令>` | shell 脚本 / agent 的 bash 工具 |
| MCP（`python -m openlight.mcp`） | 挂到 Codex、Claude 等支持 MCP 的 agent 上 |

## 典型工作流

```
1. GET  /schema                        # 我有什么能力？42 个参数都在里面
2. POST /command  document.open        # 打开照片
3. GET  /preview                       # 看一眼现在什么样（二进制 PNG）
4. POST /command  adjustments.set_batch # 调参数，比如 {"exposure":0.6,"contrast":20}
5. GET  /preview                       # 再看一眼
6. 重复 4~5 直到满意
7. POST /command  export.image         # 导出成品
```

第一步也可以先只看某一组参数：`POST /command {"cmd":"app.schema"}` 的返回里
`groups` 给出分组，`adjustments` 给出每一项的 `id / minimum / maximum /
default / unit / description / agent_hint`。`description` 说明「它在数学上做了什么」，
`agent_hint` 说明「什么时候该用它、常见幅度是多少」——写提示词时直接抄。

调完一步就能拿到反馈，这是这个软件和普通修图软件最大的区别：
`preview.get` / `export.image` 的返回里带**画面统计**——

```json
{
  "mean_luma": 0.51,
  "clipped_highlights_pct": 2.31,
  "clipped_shadows_pct": 0.12,
  "gain": 1.516,
  "identity": false,
  "applied": ["white_balance", "exposure", "contrast", "point_curves", "hsl_mixer"]
}
```

- `clipped_highlights_pct` 大于 5%：高光被削掉了，往回收一点；
- `mean_luma` 低于 0.28：画面偏暗，可以再提亮；高于 0.72：偏亮；
- `gain` 是实际使用的线性增益（`2^EV`）；
- `applied` 列出这一步实际生效的处理级——**如果你以为调了某个参数但它不在
  `applied` 里，那就是参数还在默认值**；
- `identity` 为 true 表示当前参数全是默认值，画面逐像素等于原图。

所以不需要「猜」，可以按数据判断。

## 参数一览（42 项）

| 组 | id | 范围 | 说明 |
| --- | --- | --- | --- |
| 白平衡 | `temperature` | -100 ~ +100 | 负=偏冷（加蓝），正=偏暖（加黄橙） |
| 白平衡 | `tint` | -100 ~ +100 | 负=偏绿，正=偏洋红（比色温更敏感，小步调） |
| 基本 | `exposure` | -5 ~ +5 EV | +1 表示线性亮度翻倍 |
| 基本 | `contrast` | -100 ~ +100 | 以中灰为支点的 S 形曲线 |
| 基本 | `highlights` | -100 ~ +100 | 只动亮部，负值找回过曝细节 |
| 基本 | `shadows` | -100 ~ +100 | 只动暗部，正值找回暗部细节 |
| 基本 | `whites` | -100 ~ +100 | 白色色阶（白场位置） |
| 基本 | `blacks` | -100 ~ +100 | 黑色色阶（黑场位置） |
| 偏好 | `vibrance` | -100 ~ +100 | 自然饱和度：低饱和的加得多，已经饱和的加得少 |
| 偏好 | `saturation` | -100 ~ +100 | 统一饱和度，-100 就是黑白 |
| 色调曲线 | `tone_highlights` | -100 ~ +100 | 最亮那一段的位移 |
| 色调曲线 | `tone_lights` | -100 ~ +100 | 亮调区 |
| 色调曲线 | `tone_darks` | -100 ~ +100 | 暗调区 |
| 色调曲线 | `tone_shadows` | -100 ~ +100 | 最暗那一段 |
| 曲线 | `curve_rgb` | 控制点 | 作用于整体亮度，先执行 |
| 曲线 | `curve_red` / `curve_green` / `curve_blue` | 控制点 | 单通道曲线，后执行 |
| HSL 混色器 | `hsl_<色带>_<属性>` | -100 ~ +100 | 色带：red/orange/yellow/green/aqua/blue/purple/magenta；属性：hue/sat/lum |

HSL 混色器一共 8×3 = 24 个参数，例如 `hsl_blue_lum`（压暗蓝色天空）、
`hsl_orange_sat`（加浓肤色）。**色相和饱和度是相对量，明亮度是加权量**：
明亮的权重乘了饱和度，所以灰色/接近灰的像素不会被某个色带的滑块带走。

### 曲线怎么传

曲线参数的值是控制点列表，x 是输入亮度、y 是输出亮度，都在 0~1，x 必须递增：

```json
{"cmd": "curve.set_points",
 "params": {"id": "curve_rgb", "points": [[0,0],[0.25,0.18],[0.75,0.82],[1,1]]}}
```

`[[0,0],[1,1]]` 是直线（不改变画面）。插值用单调三次样条（Fritsch-Carlson），
不会过冲、不会反相，所以你可以放心写「S 形」「抬黑」这类形状：

| 想要的效果 | 控制点 |
| --- | --- |
| 提亮中间调 | `[[0,0],[0.5,0.6],[1,1]]` |
| 加对比（S 形） | `[[0,0],[0.25,0.18],[0.75,0.82],[1,1]]` |
| 褪色感（抬黑压白） | `[[0,0.08],[1,0.94]]` |
| 反向 S（柔和） | `[[0,0],[0.3,0.37],[0.7,0.63],[1,1]]` |

## 命令一览

完整的、带参数说明的清单永远以 `GET /schema` 为准（代码在
`openlight/api/catalog.py`）。常用命令：

```
app.info              软件信息 + 服务地址
app.schema            自述文档（命令 + 调整项）
document.open         {"path": "D:\\a.jpg"}
document.close        关闭当前照片（换一张继续编辑时用）
document.state        当前照片与参数
adjustment.set        {"id": "exposure", "value": 0.5}      任意参数（曲线也可以）
adjustment.reset      {"id": "exposure"}
curve.set_points      {"id": "curve_rgb", "points": [[0,0],[0.5,0.6],[1,1]]}
adjustments.set_batch {"adjustments": {"exposure": 0.5, "contrast": 20}}
adjustments.reset_all
history.undo / redo   和人共用同一个历史栈
history.state
preview.get           base64 预览 + 统计
preview.save          预览存成 PNG 文件（推荐，省带宽）
export.image          {"path": "D:\\out.jpg", "overwrite": false}
recipe.save           sidecar 配方（<文件>.openlight.json）
recipe.reload         从 sidecar 重新载入（覆盖当前参数，一步历史）
events.list           最近发生了什么（含人的手动操作）
```

## 需要注意的几点

1. **两种量纲**。`exposure` 是 EV（+1 表示亮度翻倍，常见 ±0.3 ~ ±1.5，
   不要一上来给 ±5）；其余标量都是 -100 ~ +100 的相对量，先按 ±10 ~ ±30 试。
2. **不会覆盖原图**。`export.image` 拒绝把输出写到源文件上；目标文件已存在时
   默认报错，需要显式传 `"overwrite": true`。
3. **超范围会被夹住**。返回里 `clamped: true` 表示你给的值越界了。
4. **事件日志会同时记录人的操作**。如果用户在界面上手动拖了滑块，
   你会在 `events.list` 里看到 `source: "gui"` 的记录。如果要和人的操作协作，
   就靠这个同步。
5. **失败永远有结构**：`{"ok": false, "error": {"code", "message", "hint"}}`。
   按 `code` 判断，按 `hint` 纠正。
6. **撤销是共享的**。你撤销的是「最近一步」，不管那一步是谁做的。

## 错误码

| code | 含义 | 怎么办 |
| --- | --- | --- |
| `unknown_command` | 命令名不对 | 调 `app.schema` |
| `unknown_adjustment` | 调整项 id 不对 | 调 `app.schema`，返回里会列出全部可用 id |
| `bad_request` | 参数缺失或格式不对 | 看 `hint`；曲线要传 `[[x,y],...]` |
| `not_a_curve` | 对非曲线参数调用 `curve.set_points` | 用 `adjustment.set` |
| `no_document` | 还没打开照片 | 先 `document.open` |
| `file_not_found` | 路径不对 | 检查路径；JSON 里的反斜杠要写成 `\\` |
| `unsupported_format` | 格式不支持 | 支持 jpg/png/tif/webp/bmp/gif |
| `out_of_range` / `clamped` | 数值越界 | 用返回里的边界值 |
| `file_exists` | 输出文件已存在 | 换路径或加 `overwrite` |
| `nothing_to_undo` | 没得撤了 | 正常情况，忽略即可 |
| `internal_error` | 软件内部错误 | 把 `message` 反馈给开发者 |

## 调参的路子（写给要一次调好的 agent）

1. 先 `preview.get` 拿到 `mean_luma` 和两个削波比例，判断大方向：
   偏暗就 `exposure` 正、偏亮就负，一次 0.3 ~ 0.7 EV。
2. 过头了再收：高光削波多 → `highlights` 负值；暗部糊成一片 → `shadows` 正值。
3. 画面「灰、没层次」→ `contrast` +15 ~ +35，或者给一条 S 形曲线。
4. 偏色 → `temperature` / `tint` 先主体白平衡，再考虑单通道曲线。
5. 局部颜色 → HSL 混色器：橙/红管肤色，蓝/青管天空，绿/黄管植物。
6. 最后整体浓淡 → `vibrance`（温和）或 `saturation`（猛烈）。
7. 每改一轮就再看一次图（`openlight_see_image` 或多模态看图），
   确认「看上去是不是我要的效果」——统计数字只解决曝光，审美得靠看。

一次设置多个参数用 `adjustments.set_batch`（一步历史），
要分步微调就一条条来，方便单个撤销。

## 让人和 agent 一起工作

- 用户在界面上拖滑块 → 事件 `adjustment.changed`、`source: "gui"`；
- agent 调 API → 同一事件、`source: "agent"`；
- 界面底部的「Agent 控制台」把两边都实时显示出来，颜色区分来源。

## 扩展方向（给下一个开发者 / agent）

加一个调整项只需要两步：

1. 在 `openlight/core/adjustments.py` 注册（范围、默认值、语义说明）；
2. 在 `openlight/core/engine.py` 里实现像素运算。

命令层不用改——`adjustment.set` 会自动认识它，`app.schema` 会自动把它
告诉 agent，界面上的滑块也会自动多出来一行。这是「agent 友好」在架构上的兑现：
**能力只描述一次，人和 agent 同时获得。**
