# 推送转发 · astrbot_plugin_push_forwarder

接收外部系统的 HTTP 推送，把消息分发到**多个机器人实例**的**群聊或私聊**。
转发目标在 AstrBot WebUI 的可视化面板里勾选，每个机器人可以单独设置**转发时段**。

## 功能

- **HTTP 推送接收**：独立端口 + Token 鉴权，兼容多种字段命名与 `text/plain`、GET 传参
- **多机器人分发**：一条推送同时发往多个机器人下的多个会话，按机器人分组并发、组内限速
- **可视化目标选择**：WebUI 面板中勾选群/私聊，无需手抄群号
- **按机器人设置转发时段**：星期 + 多时间段 + 时区，支持跨零点；目标可继承或独立设置
- **超时段处理**：排队补发（可合并）/ 直接丢弃 / 不限制，队列持久化，重启不丢
- **标签路由**：给目标打标签，推送时指定标签只发给匹配的目标
- **指定机器人**：推送里写 `bots` 只发给指定的机器人；写在 URL 参数里也算，只能改地址的推送方也能用
- **@ 提醒**：@全体 或 @指定成员，按平台能力自动降级
- **推送记录**：每条推送对每个目标的成败与原因都可在面板查看

## 安装

把整个 `astrbot_plugin_push_forwarder` 目录放到 AstrBot 的 `data/plugins/` 下，
在 WebUI 的「插件管理」中重载插件即可。无需额外安装依赖。

要求 AstrBot **v4.17.0** 及以上（可视化面板依赖插件 Pages 特性）。
若版本较低，插件会自动降级为「指令 + 配置」模式，功能仍可用，只是没有面板。

## 快速开始

**1. 让机器人发现目标会话**

QQ 官方机器人和微信 ClawBot 都**无法查询群/好友列表**，插件只能从收到的消息中发现会话。
所以先做以下任一操作：

- 在想接收推送的群里 @机器人 说句话，或让机器人收到一条私聊消息
- 或者直接在该会话中发送指令 `/fwd here`（管理员权限），一步到位

**2. 在面板里勾选目标**

打开 AstrBot WebUI → 插件管理 → 找到「推送转发」→ 打开它的页面：

- **左栏点一行就是选中该机器人**，中栏和右栏跟着切换；行内的「时段」按钮设置该机器人的转发时段
- 中栏切换群聊/私聊，勾选要转发的会话
- 右栏给目标设标签、@ 方式，点「保存」

> 面板是 AstrBot 的插件 Page，不是「插件配置」那个表单。插件配置里只有端口、模板一类
> 全局参数，选机器人和选会话都在面板里。若 WebUI 里找不到面板入口，多半是 AstrBot
> 版本低于 4.17；这时用下面的指令一样能配完。

**3. 拿到推送地址**

面板顶部直接给出**带 Token 的完整地址**，点「复制完整地址（含 Token）」就能粘到任何
只让填一个 URL 的推送工具里：

```
http://你的服务器IP:9966/push?token=你的Token
```

旁边的「主机」输入框用来改生成地址里的主机名 —— 从内网打开 WebUI、推送方却在别的网段时
按需填写，只影响这里生成的地址，不改服务端监听。

不开面板也可以：私聊机器人发送 `/fwd url` 会直接返回完整地址。

**4. 推送消息**

点「复制 curl 示例」即可拿到可直接运行的命令：

```bash
curl -X POST http://你的服务器IP:9966/push -H "X-Token: 你的Token" -H "Content-Type: application/json" -d '{"title":"服务器告警","text":"CPU 使用率 95%","tags":["alert"]}'
```

## 推送接口

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/push` | 主入口，JSON 请求体 |
| POST | `/push` | `Content-Type: text/plain` 时整个请求体作为正文 |
| GET | `/push?token=&text=` | 便于 curl、浏览器和简易脚本调用 |
| POST | `/push/<标签>` | 用路径指定标签 |
| POST | `/push?bot=<机器人>` | 用 URL 参数指定机器人与标签，请求体不用改 |
| POST | `/push?key=<token>` | 企业微信群机器人格式，见下文 |
| GET | `/health` | 健康检查，**同样需要 Token** |

**没有任何免鉴权入口。** 其余路径一律返回 404，且不透露这里跑着什么服务。

### 鉴权

以下任一方式均可，插件用常数时间比较：

- 请求头 `X-Token: <token>`
- 请求头 `Authorization: Bearer <token>`
- 查询参数 `?token=<token>`
- 查询参数 `?key=<token>`（企业微信群机器人用的参数名，等价于 `token`）
- JSON 请求体中的 `token` 字段

Token 在插件首次加载时自动生成并写入配置，可在面板或插件配置中查看修改。
另可配置来源 IP 白名单（支持 CIDR），白名单先于 Token 校验。

**把配置里的 Token 清空不等于关闭鉴权，而是拒绝所有请求**（返回 503）。
要停用服务请关掉「启用 HTTP 推送接收服务」，别靠清空 Token。

### 请求字段

```json
{
  "title": "服务器告警",
  "text": "CPU 使用率 95%",
  "tags": ["alert"],
  "bots": ["qq_bot_1"],
  "targets": ["t_a1b2c3"],
  "at": { "mode": "all" },
  "urgent": false
}
```

| 字段 | 兼容写法 | 说明 |
| --- | --- | --- |
| `title` | `subject`、`summary`、`标题` | 标题，可省略 |
| `text` | `content`、`message`、`body`、`msg`、`desc`、`description`、`内容` | 正文 |
| `tags` | `tag`、`group`、`channel`、`标签` | 字符串或数组，逗号分隔亦可 |
| `targets` | `target`、`to` | 直接指定目标编号，指定后忽略标签路由 |
| `bots` | `bot`、`bot_id`、`bot_ids`、`platform_id`、`机器人` | 只发给这些机器人。字符串或数组，逗号分隔亦可；写实例 id 或面板上的备注 |
| `at` | — | `true` / `"all"` / `["用户ID"]` / `{"mode":"users","users":[...]}` |
| `urgent` | `force` | `true` 时忽略转发时段立即发送 |
| `dry_run` | `dryrun`、`dry-run` | `true` 时只试不发：解析、标签路由、时段判定照常走完，但不发送、不入队、不写推送记录。响应里 `summary.dry_run` 是这条推送会命中的目标数 |

`title` 与 `text` 至少要有一个，否则返回 400。

布尔字段按字面判：`?dry_run=0`、`?urgent=false` 都是假，不会因为查询串里全是字符串就被当成真。

### 接通之后怎么验

三步，一步比一步深，出问题就停在那一步：

```bash
# 1. 端口通不通、Token 对不对（不碰分发器，随便点）
curl -s "http://宿主机IP:9966/health?token=你的Token"

# 2. 整条链路走一遍，但不真发到群里；看 summary.dry_run 是几
curl -s -X POST "http://宿主机IP:9966/push?token=你的Token"   -H "Content-Type: application/json" --data-binary @push.json

# 3. 去掉 dry_run，群里应该就收到了
```

`push.json`（**存成 UTF-8**）：

```json
{"title":"服务器告警","text":"CPU 使用率 95%","tags":["alert"],"dry_run":true}
```

第 2 步返回 `"dry_run": 1` 表示会命中 1 个目标；返回 `0` 说明目标没配、被停用，或者标签对不上
—— 这正是「推送返回成功但群里没消息」最常见的原因。

Windows 上别把中文直接写在 `-d '…'` 里：cmd / PowerShell 会按 GBK 编码命令行，
服务端会收到 `'utf-8' codec can't decode byte 0xb7`。要么像上面那样用 UTF-8 文件，
要么用 PowerShell 显式转字节：

```powershell
$body = @{ text='CPU 使用率 95%'; dry_run=$true } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://宿主机IP:9966/push' -Method Post `
  -Headers @{ 'X-Token'='你的Token' } -ContentType 'application/json' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

### 对接「企业微信机器人」通道

很多监控和告警系统（Grafana、告警平台、自研系统…）自带「企业微信机器人」推送通道，
界面上只让填一个 Webhook 地址。本插件兼容这套协议，**把那个地址换成本插件的地址即可，
推送方的代码和消息体都不用改**：

```
原来： https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-xxxx-xxxx
换成： http://你的服务器IP:9966/push?key=<本插件的Token>
```

把面板顶部复制到的地址里的 `?token=` 改成 `?key=` 即可，两个参数名等价。

识别方式是消息体里的 `msgtype` 字段，无需任何额外配置。支持的类型：

| msgtype | 处理方式 |
| --- | --- |
| `text` | `text.content` 作为正文 |
| `markdown` / `markdown_v2` | `markdown.content` 作为正文；`<@userid>` 还原成 `@userid`，`<font color=…>` 标签去掉 |
| `news` | 图文渲染成「标题 + 摘要 + 链接」；只有一条时标题提升为 `title` |
| `template_card` | 尽力抽取标题、描述、键值行与跳转链接 |
| `image` / `file` / `voice` | **不支持**，返回 `errcode: 40008`（本插件只转发文本） |

其余 markdown 语法（`**粗体**`、`[文本](链接)`、`> 引用`）原样透传 —— 目标平台只能发纯文本，不做二次渲染。

**@ 的映射规则**（企微用 userid 和手机号标识人，QQ、微信是另一套 ID，对不上号）：

- `mentioned_list` 或 `mentioned_mobile_list` 里出现 `@all` → 映射成本插件的 `@全体`
- 其余 `mentioned_list` 里的 userid → 退化成正文前缀 `@wangqing`，信息不丢但不是真 @
- 其余 `mentioned_mobile_list` 里的手机号 → **直接丢弃**，不把号码转发到另一个群里

**响应格式也按企微的来**，因为那类通道普遍只看 `errcode` 判成败：

```json
{ "errcode": 0, "errmsg": "ok", "data": { "summary": { "sent": 2 } } }
```

出错时 `errcode` 非零，**同时** HTTP 状态码也按语义返回（企微自己一律回 200）。
这样只看状态码和只看 `errcode` 的推送方都能发现失败。

本插件自己的扩展字段可以直接混在企微消息体里，用来按标签分流、指定机器人：

```json
{
  "msgtype": "text",
  "text": { "content": "CPU 95%", "mentioned_list": ["@all"] },
  "tags": ["alert"],
  "bots": ["qq_bot_1"],
  "urgent": true
}
```

推送方的界面只让填一个 URL、消息体改不了的话，把这些字段写成 URL 参数也一样：
`...?key=<Token>&bot=qq_bot_1&tags=alert`，详见上面的「指定机器人」。

> 注意 `?key=` 会进 Nginx 访问日志和浏览器历史。推送方支持自定义请求头的话，
> 优先用 `X-Token` 请求头。

### 指定机器人

`bots` 把本次推送限定在几台机器人上，
**它是在标签路由之上再筛一层，不是替代它**：

```json
{ "text": "CPU 95%", "tags": ["alert"], "bots": ["qq_bot_1"] }
```

上面这条只会发给 `qq_bot_1` 下打了 `alert` 标签的目标。
同时给了 `targets` 时取交集，被筛掉的目标会在响应里说明原因。

**该写什么**：机器人的**实例 id**，也就是 AstrBot
「消息平台」里那个实例的名字。面板左栏的机器人卡片上，
鼠标悬停就能看到「实例 id」—— 卡片上显示的大字是备注，不一定等于 id。

大小写对不上也认；实例 id 没匹配上时会再试一次面板上的备注，
**但备注重名时直接当作没找到**—— 宁可报错，也不能猜一个发到别人的群里。

写了机器人但一个都没匹配上时，**这条推送一个目标也不会发**（而不是
退回全发），响应的 `results` 里会区分两种情况：

| detail | 怎么修 |
| --- | --- |
| 指定的机器人不存在（实例 id 与备注都没匹配到） | id 写错了，对着面板改 |
| 该机器人下没有命中本次推送的转发目标 | id 对的，但那台机器人没配目标，或标签没对上 |

**只能改地址的推送方（企微通道那类）写在 URL 上**：

```
http://你的服务器IP:9966/push?key=<Token>&bot=qq_bot_1
```

`bots` / `tags` / `targets` / `urgent` / `dry_run` 这些**路由字段**都可以写在查询串里，
请求体一行不用改。请求体里有同名字段时以请求体为准。
同名参数写多次（`?bot=a&bot=b`）或逗号分隔（`?bot=a,b`）都行。

> 正文相关的字段（`text`、`title`）**不**从 URL 取（GET 除外）。
> 让 URL 能顶掉请求体里的正文，只会制造"发出去的内容和推送方以为的不一样"这类难查的问题。

### 标签路由规则

- 推送**不带**标签：发给所有启用的目标
- 推送**带**标签：只发给标签有交集的目标；未设标签的目标不会收到

### 响应

```json
{
  "status": "ok",
  "data": {
    "summary": { "sent": 2, "queued": 1, "skipped": 0, "failed": 0 },
    "results": [
      { "target_id": "t_a1b2c3", "display_name": "运维告警群", "status": "sent", "detail": "已发送" },
      { "target_id": "t_d4e5f6", "display_name": "张三", "status": "queued", "detail": "不在转发时段，将于 09-01 22:00 补发" }
    ]
  }
}
```

有目标被送出或排队时返回 200，全部被跳过时返回 202；鉴权失败 401，IP 不允许 403，
内容不合法 400。

## 暴露到公网前

这个端口是插件自己监听的，不受 AstrBot Dashboard 的鉴权保护，所以做了这些约束：

- **所有路径都要过鉴权**，包括 `/health`；未知路径统一 404，不回显服务名和推送路径
- **未授权的请求看不到任何细节**：JSON 写错了也只回 401，不会告诉对方哪里错
- Token 用 `secrets.compare_digest` 常数时间比较，自动生成的是 32 位随机串
- 请求体上限 256 KB，未授权请求同样受限
- 拒绝日志按分钟节流，被扫描时不会把日志刷爆，但累计次数照记

仍然建议：

- **优先用 IP 白名单**把来源限制到推送方的地址或网段，这是最有效的一层
- 只在需要外网推送时才把端口映射/放行出去；同机推送把「监听地址」改成 `127.0.0.1`
- 需要 HTTPS 就在前面挂一层 Nginx / Caddy 反代，插件本身只提供 HTTP

### 走反向代理（推荐）

已经有 Nginx / Caddy 挡在 AstrBot 前面时，把推送路径也反代过去，就不用再对外开 9966，还白捡一个 HTTPS：

```caddyfile
astr.example.com {
    # 推送接口走插件自己的端口
    handle /push* {
        reverse_proxy 127.0.0.1:9966
    }
    handle /health {
        reverse_proxy 127.0.0.1:9966
    }
    # 其余仍然是 AstrBot 面板
    handle {
        reverse_proxy 127.0.0.1:6185
    }
}
```

这么配之后，把面板顶部的「主机」框填成 `https://astr.example.com`（带协议的完整前缀），生成的推送地址就会是 `https://astr.example.com/push?token=…`，不再带 9966 端口。

反代方式下 Docker **不需要**映射 9966，接收服务监听地址还可以收窄成 `127.0.0.1`。

## 转发时段

时段配置在**每个机器人**上，转发目标默认继承所属机器人，也可以取消继承单独设置。

- **星期**：多选，ISO 星期（周一到周日）
- **时间段**：可添加多段；结束时间早于开始时间表示跨零点，如 `22:00 - 02:00`
- **时区**：填 IANA 名称（`Asia/Shanghai`）或固定偏移（`UTC+8`、`+08:00`）
- **时段外的消息**：
  - `排队` —— 存入队列，进入时段后由后台任务（每分钟检查一次）补发；
    默认会把积压的多条合并成一条，避免刷屏
  - `丢弃` —— 直接丢弃并记录
  - `不限制` —— 照常发送

队列持久化在 `data/push_forwarder/queue.json`，AstrBot 重启后不会丢失。
推送时带 `"urgent": true` 可以绕过时段限制。

> Windows 上 Python 默认不带 IANA 时区数据库。插件内置了常用时区的偏移兜底，
> `Asia/Shanghai` 这类名称照常可用；若填了冷门时区且系统无 `tzdata`，会回退到本机时区。

## 平台能力

不同适配器的能力差异很大，插件会在面板上禁用无效选项，并在发送时自动降级：

| 平台 | 群聊 | 私聊 | @ 提醒 | 会话列表 |
| --- | --- | --- | --- | --- |
| QQ 官方机器人 `qq_official` | 支持 | 支持 | 支持 | **无法查询**，靠会话发现 |
| 个人微信 `weixin_oc`（ClawBot） | **不支持** | 支持 | **不支持** | **无法查询**，靠会话发现 |
| OneBot `aiocqhttp` | 支持 | 支持 | 支持 | 支持，面板可一键拉取 |

需要特别注意的两点：

- **微信 ClawBot 适配器只处理私聊消息，也不支持 @ 组件。** 这是 AstrBot 适配器层面的限制。
  面板会禁用群聊标签页；若目标配了 @，发送时会静默跳过 @ 而正常送出文本。
- **QQ 官方机器人的群 openid 只在收到该群消息时下发**，既查不到也推算不出来，
  因此必须先让机器人在群里收到一条消息。此外腾讯对官方机器人的主动消息有频次额度，
  超额会发送失败，失败原因会记录在推送历史中。

## 指令

均需要管理员权限。

| 指令 | 说明 |
| --- | --- |
| `/fwd here [标签]` | 把当前会话添加为转发目标，可附带一个标签 |
| `/fwd rm` | 把当前会话从转发目标中移除 |
| `/fwd list` | 列出所有转发目标及积压条数 |
| `/fwd test` | 向当前会话发送一条测试消息 |
| `/fwd info` | 查看服务状态、实际监听地址与目标数量 |
| `/fwd start` | 就地重启接收服务，失败原因直接回复给你，不用翻日志 |
| `/fwd url` | 生成带 Token 的完整推送地址；在群里只显示不含 Token 的地址，需私聊获取完整版 |

`/fwd here` 是 QQ 官方群最可靠的添加方式，不需要知道 group openid。

## 配置项

在 WebUI 的插件配置中修改，改完需重载插件生效。

| 分类 | 配置项 |
| --- | --- |
| 接收服务 | 启用开关、监听地址、端口、路径、Token、IP 白名单 |
| 消息格式 | 渲染模板（`{title}` `{text}` `{tag}` `{time}`）、最大长度、超长是否分条 |
| 分发 | 机器人间并发数、同机器人发送间隔、失败重试次数、是否合并排队消息 |
| 队列与记录 | 单目标最大排队条数、保留的推送记录条数 |
| 会话发现 | 启用开关、最多记录会话数、会话过期天数 |

## 数据文件

统一放在 AstrBot 的 `data/push_forwarder/` 下，插件更新不会丢失：

| 文件 | 内容 |
| --- | --- |
| `targets.json` | 转发目标与各机器人的时段配置 |
| `sessions.json` | 已发现的会话缓存 |
| `queue.json` | 超出时段待补发的消息 |
| `history.json` | 推送历史 |

全部采用原子写入，写入中断不会损坏文件；文件损坏时会自动重命名为 `.bad` 保留现场并以空配置启动。

## 开发与测试

核心逻辑不依赖 AstrBot 运行时，可直接在本地跑：

```bash
python tests/test_schedule.py
```

```bash
python tests/test_payload.py
```

```bash
python tests/test_integration.py
```

`tests/stub_astrbot.py` 提供了一套最小的 AstrBot 运行时桩件，
集成测试会用它把插件真实加载起来，覆盖分发、时段、能力降级、面板接口与指令。

## 常见问题

**面板里看不到任何会话？**
先让机器人在目标会话中收到一条消息，或在该会话发送 `/fwd here`。
QQ 官方与微信都无法主动查询会话列表，这是平台限制。

**面板一直显示「加载中…」？**
面板会把失败原因显示在顶部横幅上，按提示排查：

- 「页面没有拿到 AstrBot 的 bridge SDK」——不是从 WebUI 的插件面板入口打开的，
  或 AstrBot 版本低于 4.17（插件 Pages 是这个版本加的）。用 `/fwd` 指令一样能配完。
- 「调用接口 xxx 失败：HTTP 404」——后端接口没注册上。看 AstrBot 日志里有没有
  `[push_forwarder] 已注册 N 个面板接口`；没有的话说明当前版本的 `register_web_api`
  不兼容，日志里会有具体报错。
- 「调用接口 xxx 失败：HTTP 401/403」——Dashboard 登录态失效，重新登录 WebUI。

接口连通性可以单独验一下：面板打开时浏览器访问
`/api/v1/plugins/extensions/astrbot_plugin_push_forwarder/ping` 应返回 `{"status":"ok",...}`。

**推送返回成功但群里没消息？**
看返回的 `summary`：`queued` 说明不在转发时段（等待补发或改时段），
`skipped` 会在 `results[].detail` 里给出原因（目标停用、平台不支持等），
`failed` 常见于 QQ 官方主动消息额度用尽。

**配置的端口没有被监听？**
接收服务是**独立于 AstrBot WebUI 的另一个端口**，两者必须不同 —— 填成 AstrBot
自己的端口（默认 6185）会因为端口已被占用而 bind 失败。默认 9966，随便挑一个空闲端口即可。

按顺序排查：

1. 看 AstrBot 日志有没有这行：
   `[push_forwarder] 推送接收服务已启动：http://0.0.0.0:9966/push（实际监听 0.0.0.0:9966）`
   有 `实际监听` 说明端口在 AstrBot 进程里确实开了。
2. 没有这行，就找 `[push_forwarder] 端口 xxx 没能监听起来`，后面跟着具体原因。
   也可以私聊机器人发 `/fwd start` 就地重试，失败原因会直接回给你。
3. 日志说已启动、但从别的机器连不上：
   - **AstrBot 跑在 Docker 里**：容器内监听不等于宿主机监听。需要在 `docker-compose.yml`
     的 `ports` 里加一行 `- "9966:9966"`（或 `docker run -p 9966:9966`）后重建容器。
     这是这个问题最常见的原因。
   - 服务器防火墙 / 安全组没放行该端口。
   - `监听地址` 被改成了 `127.0.0.1`，那样只有本机能连。
4. 插件配置里「启用 HTTP 推送接收服务」被关掉了 —— 面板顶部会有黄色横幅提示。

映射通了之后这样验证（`/health` 也要带 Token）：

```bash
curl -i -H "X-Token: 你的Token" http://宿主机IP:9966/health
```

返回 `{"status":"ok","data":{"running":true}}` 即为打通。

**重载插件后提示端口被占用？**
插件在 `terminate()` 里会释放端口。若仍被占用，多半是上一次进程未正常退出，
可改用其他端口，或重启 AstrBot。
