# 更新日志

本项目所有重要的变更都记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- TTFT / 总耗时 / 吞吐指标：每次请求输出一行 `[ttft]` 日志（首内容 token 延迟、总耗时、输出 token、tok/s）。
- 本地只读接口 `GET /stats`：返回最近 `STATS_WINDOW`（默认 200）条样本的整体与按模型聚合（avg / p50 / p95 / min / max / errors）。

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

[Unreleased]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/LIMTCYT/opencode-go-proxy-for-trae/releases/tag/v1.0.0
