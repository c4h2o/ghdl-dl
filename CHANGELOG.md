# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

## [1.0.0] - 2026-11-10

首个公开版本。

### 新增

- **多连接分段下载**：按 HTTP `Range` 把文件切成若干段，N 个线程并行下载，先完成的线程继续领新段。
- **断点续传**：每段进度持久化到 `.ghdlpart.json`（fsync + 原子替换），暂停、崩溃、重启后从断点继续。
- **断线自动重连**：默认无限重试、指数退避（1s → 30s）；有进展时立即续传，不空等。
- **签名地址自动刷新**：GitHub CDN 地址过期（403/404）时自动回原始链接换取新地址，多线程共享刷新结果。
- **数据校验**：支持 `--sha256`；GitHub Release 资源可自动读取官方 SHA-256；校验失败保留数据。
- **两种落盘模式**：默认预分配单文件按偏移写入（无需二次拼接）；`--parts` 每段一个文件，完成后拼接。
- **单流降级**：不支持 `Range` 的链接（如 GitHub 源码包）自动退化为单连接顺序下载。
- **图形界面**：任务列表、进度/速度/ETA、暂停/继续/重试、任务持久化、启动时自动续传未完成任务。
- **网络适配**：HTTP(S) 代理（含 CONNECT 与代理认证）、`HTTPS_PROXY` 环境变量、GitHub Token。
- **Windows 适配**：文件锁、DPI、UTF-8 控制台；提供双击启动的 `start-gui.bat` / `start-cli.bat`。
- **应用图标**：SVG 源文件、1024 PNG、Windows 多尺寸 ICO，以及 GitHub 社交预览图。
- **启动脚本使用英文文件名**，方便非中文用户和命令行环境。
- **17 个自动化测试**：本地故障注入服务器覆盖断流、签名过期、503 恢复、无 Range、分片拼接、暂停续传、SHA-256 不匹配等场景。

### 已知限制

- GitHub 源码包（`/archive/...`）由服务器动态生成，不支持 `Range`，断线后只能从头下载。
- 单文件模式依赖本地文件系统；把 `.ghdlpart` 放在网络盘上时，随机写性能可能较差，建议改用 `--parts`。
- 图形界面目前只有中文。

[Unreleased]: https://github.com/c4h2o/ghdl-dl/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/c4h2o/ghdl-dl/releases/tag/v1.0.0
