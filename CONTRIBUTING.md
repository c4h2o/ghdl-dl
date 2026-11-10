# 贡献指南

感谢愿意一起改进 ghdl-dl。它是一个**纯标准库**的小工具，保持依赖为零是刻意的设计，
所以新增功能时请优先使用 Python 标准库。

## 开发环境

需要 Python 3.9+。图形界面用 `tkinter`，Windows 官方安装包自带；Debian/Ubuntu 需要
`sudo apt install python3-tk`。

```bash
git clone https://github.com/c4h2o/ghdl-dl.git
cd ghdl-dl

# 不安装也能直接跑
python run_cli.py --help
python run_gui.py

# 跑测试（约 10 秒，本地起 HTTP 服务器，不联网）
python -m unittest discover -s tests -t . -v
```

安装为命令（可选）：

```bash
pip install -e .
ghdl-dl --version
```

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `ghdl/engine.py` | 分段、线程池、续传、重试、校验、组装；**改动必须配测试** |
| `ghdl/http_client.py` | 重定向、Range、代理、Token、TLS |
| `ghdl/probe.py` | 探测大小/是否支持 Range/文件名/GitHub API 摘要 |
| `ghdl/writer.py` | 单文件随机写、分片写、单流写 |
| `ghdl/model.py` | 分段模型、状态持久化、文件锁 |
| `ghdl/cli.py` | 命令行与进度显示 |
| `ghdl/gui.py` | tkinter 界面（只在主线程碰控件） |
| `tests/server.py` | 故障注入测试服务器：断流、签名过期、限流、无 Range |
| `tools/make_icon.py` | 重新生成图标与社交预览图（Pillow） |
| `ghdl/assets/` | 图标（SVG/PNG/ICO）与社交预览图 |

## 提交规范

- 一个 PR 只做一件事，避免“顺手重构 + 新功能”混在一起。
- Commit message 建议用 `feat:` / `fix:` / `docs:` / `test:` / `chore:` 前缀，中英文均可。
- 修 bug 时尽量先写一个能复现的测试。

## 测试要求

- 引擎、HTTP 层、续传逻辑的改动：必须新增或更新 `tests/test_engine.py` 中的用例。
- 测试不能依赖外网；需要特殊服务端行为时，在 `tests/server.py` 里加路由。
- 涉及 Windows 特有行为（文件锁、路径、CRLF）时，请在 PR 说明里注明是否在 Windows 上验证过。
- 改 GUI 的请在 PR 里附截图。

## 代码风格

- 4 空格缩进；行宽尽量 ≤ 110，长行以可读性优先。
- 使用 `from __future__ import annotations` 和类型标注。
- 用户可见文案用中文；错误信息要告诉用户“下一步能做什么”。
- 不要在日志里输出 Token、完整签名 URL、代理密码。
- 线程内不要直接操作 tkinter 控件，统一走 `queue.Queue`（参考 `gui.py`）。

## 不要提交

- Token、`.env`、私有仓库真实链接；
- 下载临时文件（`*.ghdlpart`、`*.ghdlpart.json`、`*.ghdlparts/`）；
- 构建产物（`dist/`、`build/`、`*.egg-info/`）；
- 大文件（截图请压到几百 KB 内）。

`.gitignore` 已经覆盖了这些；如果发现漏网，请顺手补一条。

## 发布

维护者发布流程见 [docs/RELEASING.md](docs/RELEASING.md)。
