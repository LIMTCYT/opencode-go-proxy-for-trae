# opencode-go-proxy-for-trae

本地反向代理：在 Trae 与 OpenCode Go (Zen API) 之间做双向转发，自动注入上游要求的会话/请求头（`x-opencode-session` 等），并做模型名别名重写。解决 Trae 直连时报 `Request is missing x-opencode-session` 400 错误的问题。

纯 Python 标准库实现，无需安装任何第三方依赖，Python 3.6+ 即可运行。

## 适用
**适用**：trae，treaCN，dsh等 亲测有效

**接口格式：** 走 OpenAI 兼容协议（`POST /chat/completions`，请求体与响应体均为 OpenAI 格式），因此 Trae 以及任何 OpenAI 兼容的客户端都能直接接入。

OpenCode Go 官方文档：<https://opencode.ai/v2/docs/console/go/>

> [!IMPORTANT]
> **近期更新：** opencode 的 Go 接口前缀已由 `/zen/go/v1` 切换为 `/inference/go/v1`，但旧路由仍在上游存在。本代理按客户端路径原样透传，Trae 里填新地址或旧地址都能正常工作；同时注意 **model ID 要和请求地址配套**，填错会报 404。例如新 model ID `deepseek-v4.1-flash` 走 `/inference/go/v1`，老 model ID `deepseek-flash` 走 `/zen/go/v1`。详见下方[配置说明](#2-配置-trae-自定义模型)。完整变更见 [更新日志](CHANGELOG.md)。

## 使用步骤

### 1. 启动代理

```bash
python opencode-go-proxy-for-trae.py
```

看到如下日志即启动成功：

```
[opencode-go-proxy] listening on http://127.0.0.1:9355
[opencode-go-proxy] upstream -> https://opencode.ai
```

### 2. 配置 Trae 自定义模型

| 配置项 | 填写内容 |
|---|---|
| 请求地址（Base URL） | `http://127.0.0.1:9355/inference/go/v1`（新）或 `http://127.0.0.1:9355/zen/go/v1`（旧），二者均可用 |
| 模型 ID | `proxy-` + 真实模型名，例如 `proxy-glm-5.3-flash` |
| API Key | 你的 OpenCode API Key |

**关于请求地址：** opencode 已把 Go 接口前缀由 `/zen/go/v1` 切换为 `/inference/go/v1`，但上游两条路由都还在。代理按客户端路径原样透传，所以 Trae 里填旧地址或新地址都能正常工作。

**重要：模型 ID 要和请求地址配套，填错会报 model not found / 404。** 部分老 model ID 只在旧路由 `/zen/go/v1` 提供，新 model ID 只在新路由 `/inference/go/v1` 提供：

| 模型 | model ID（加 `proxy-` 前缀） | 对应请求地址 |
|---|---|---|
| 新模型 | `proxy-deepseek-v4.1-flash` 等 | `http://127.0.0.1:9355/inference/go/v1` |
| 老模型 | `proxy-deepseek-flash` 等 | `http://127.0.0.1:9355/zen/go/v1` |

在 Trae 里按模型所属路由填对应的 Base URL，才能落到正确的上游路由。

**重要：模型 ID 必须加 `proxy-` 前缀。** 如果与官方预设重名（如直接填 `glm-5.3-flash`），Trae 会把对话流量路由到自己的云端通道，完全绕过本代理。代理会把前缀剥掉，还原为真实模型名再发给上游。

### 3. 开始对话

正常在 Trae 中使用即可，无需其他操作。

## 常用环境变量（可选）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROXY_PORT` | `9355` | 监听端口 |
| `UPSTREAM_HOST` | `opencode.ai` | 上游主机（可带端口，如 `example.com:8443`） |
| `UPSTREAM_PROTO` | `https` | 上游协议（`https` / `http`） |
| `OPENCODE_UA` | `opencode/1.18.29 cli` | 注入的 User-Agent |
| `LOG_FILE` | 脚本目录下 `data\opencode-go-proxy.log` | 日志文件路径 |
| `PID_FILE` | 脚本目录下 `.opencode-go-proxy-for-trae.pid` | pid 文件路径 |

示例（PowerShell）：

```powershell
$env:PROXY_PORT = "9356"
python opencode-go-proxy-for-trae.py
```

端口变了的话，Trae 请求地址相应改为 `http://127.0.0.1:9356/zen/go/v1` 或 `http://127.0.0.1:9356/inference/go/v1`（按模型所属路由选）。

## 日志与排错

- 日志同时输出到控制台和 `data\opencode-go-proxy.log`（超过 5MB 自动轮转，最多保留 2 个备份）。
- 对话报错时，先看日志中的 `[upstream-error] body:` 行，里面有上游返回的具体错误信息。
- 若 Trae 的请求没有出现在本代理日志里，多半是模型 ID 没加 `proxy-` 前缀，流量被 Trae 云端通道接走了。
- 停止代理：控制台按 `Ctrl+C`。

## 工作原理（简述）

每次请求转发前，代理自动注入：

- `x-opencode-session`：会话 ID（同一对话保持稳定，用于上游提示词缓存路由）
- `x-opencode-request`：请求 ID（同一会话内递增，如 `msg_1`、`msg_2`）
- `x-opencode-client`：`cli`
- `x-opencode-project`：`global`

会话识别优先级：客户端显式会话标识（header/body）> 内容哈希（system + 首条 user 消息）> 随机临时会话。
