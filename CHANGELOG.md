# 更新日志

本项目所有重要的变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 上游 5xx 自动重试（`RETRY_ON_5XX_MAX`，默认 `5`）：上游返回 5xx（例如响应体为空的 `503`）时，只要尚未向客户端吐出任何数据，就退避 1 秒后重发，客户端无感；`4xx`（含 `429` 限流）不重试，设为 `0` 可关闭。

### Changed

- 控制台日志配色：上游返回非 200（4xx/5xx）的日志行改为**黄色**高亮，便于一眼定位失败请求。

## [1.2.0] - 2026-10-21

### Added

- 慢路径心跳保活：上游超过 `HEARTBEAT_AFTER`（默认 30 秒）仍无响应头时，先回 `200` + SSE，之后每 `HEARTBEAT_INTERVAL`（默认 10 秒）发一个 `: keepalive` 注释行，避免客户端（Trae）自己先超时放弃。
- 竞速重试：到 `RETRY_AFTER`（默认 60 秒）另开一条连接重发同一请求，谁先回数据用谁，另一条连接关闭；重试硬上限 1 次（最多 2 条连接），不会无限重发。
- 内容空闲兜底 `CONTENT_IDLE_TIMEOUT`（默认 1800 秒）：距上一次内容产出（`content` / `reasoning_content` / `tool_calls`）超过该时长即主动终止请求，避免上游持续慢速输出却拿不到结果、白扣 token；设为 `0` 可关闭。
- TTFT / 总耗时 / 吞吐指标：每次请求输出一行 `[ttft]` 日志（首内容 token 延迟、总耗时、输出 token、tok/s）。
- 本地只读接口 `GET /stats`：返回最近 `STATS_WINDOW`（默认 200）条样本的整体与按模型聚合（avg / p50 / p95 / min / max / errors）。

### Changed

- 上游超时改为可配置的 `UPSTREAM_TIMEOUT`，默认 **120 秒**（原为固定的 600 秒），涵盖建连、TLS 握手、等响应头与每次读数据块，静默超过该值即判定该条连接失败。
- 超时的处理方式：等响应头超时改为立即回 `504` 并给出明确错误信息，不再让请求干等；流式中断与兜底终止会向客户端下发 SSE `error` 事件，而不是静默截断。
- 文档更新：README 新增「慢路径」「最后兜底」说明与超时参数表；路由说明提示新路由不稳定，优先使用 `/zen/go/v1`。

## [1.1.0] - 2026-10-13

### Added

- 控制台日志高亮显示。

### Changed

- 更新接口说明：OpenCode Go 接口前缀由 `/zen/go/v1` 切换为 `/inference/go/v1`（旧路由仍在上游可用），并说明 model ID 需与请求地址配套，填错会报 404。

## [1.0.0] - 2026-09-08

### Added

- 初始版本：本地反向代理，在 Trae 与 OpenCode Go 之间做双向转发。
- 自动注入上游要求的会话/请求头（`x-opencode-session` 等），解决 Trae 直连时报 `Request is missing x-opencode-session` 400 错误的问题。
- 模型名别名重写（`proxy-` 前缀剥离）。
- 纯 Python 标准库实现，无需安装任何第三方依赖，Python 3.6+ 即可运行。
- MIT License 与 `.gitignore`。

[Unreleased]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/releases/tag/v1.0.0
