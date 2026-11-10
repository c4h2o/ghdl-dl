# 发布流程

## 0. 发布前检查（重要）

- [ ] `LICENSE` 里的版权人写的是你的名字/ID（当前为 `c4h2o`）。
- [ ] `pyproject.toml` 里的 `Homepage` / `Repository` / `Issues` 是你的仓库地址。
- [ ] `README.md`、`README.en.md`、`SECURITY.md`、`.github/` 里的 `c4h2o/ghdl-dl` 已全部替换。
- [ ] `ghdl/__init__.py` 的 `__version__` 与 `CHANGELOG.md` 一致。
- [ ] 本机测试通过：

  ```bash
  python -m unittest discover -s tests -t . -v
  ```

- [ ] 真机冒烟：双击 `start-gui.bat` 能打开界面；`python run_cli.py --info <URL>` 正常。

## 1. 更新版本号与变更日志

1. 修改 `ghdl/__init__.py`：

   ```python
   __version__ = "1.1.0"
   ```

2. 在 `CHANGELOG.md` 顶部加 `## [1.1.0] - YYYY-MM-DD`，写清 Added / Changed / Fixed。

版本号遵循语义化版本：

- `PATCH`：修 bug、改文案；
- `MINOR`：向后兼容的新功能；
- `MAJOR`：命令行参数/状态文件格式不兼容。

## 2. 本地构建（可选但建议）

```bash
python -m pip install build twine
python -m build
python -m twine check dist/*
```

产物在 `dist/`：`ghdl_dl-<版本>-py3-none-any.whl` 和 `.tar.gz`。

## 3. 提交并打标签

```bash
git add -A
git commit -m "chore: release v1.1.0"
git push origin main

git tag -a v1.1.0 -m "ghdl-dl v1.1.0"
git push origin v1.1.0
```

推送 `v*` 标签后，`.github/workflows/release.yml` 会：

1. 在 Ubuntu 上构建 sdist + wheel；
2. 自动生成 Release Notes；
3. 创建**草稿** Release 并附上构建产物。

到 GitHub 的 Releases 页面检查内容，点击 **Publish release** 即可。

## 4. 附加校验和（推荐）

在发布说明里附上文件的 SHA-256，方便用户核对：

```bash
sha256sum dist/*          # Linux / macOS
Get-FileHash dist\* -Algorithm SHA256   # Windows PowerShell
```

## 5. 发布到 PyPI（可选）

包名 `ghdl-dl`。推荐使用 Trusted Publishing（OIDC，无需保存 Token）：

1. 在 PyPI 创建项目并配置 Trusted Publisher（仓库 `c4h2o/ghdl-dl`，workflow `release.yml`，
   environment `pypi`）；
2. 取消 `.github/workflows/release.yml` 末尾 `publish-pypi` 的注释；
3. 后续打标签会自动发布。

注意：PyPI 上的包名一旦发布就不能改名；如 `ghdl-dl` 已被占用，请在 `pyproject.toml` 里换成
`ghdl-downloader` 等名字，并同步修改 README 的安装命令。

## 6. GitHub 仓库设置建议

- **Description**：`Multi-connection, resumable downloader for large GitHub release assets.`
- **Topics**：`github` `downloader` `resume` `http-range` `python` `cli` `tkinter` `large-files`
- **Social preview**：上传 `ghdl/assets/social-preview.png`（Settings → General → Social preview）。
- **Branch protection**：保护 `main`，要求 CI 通过、禁止直接 push。
- **Security**：开启 Dependabot alerts、Secret scanning、Private vulnerability reporting。
- **Releases**：勾选 “Set as the latest release”；附件包含 wheel/sdist。

## 7. 每次发布前的真机清单

| 平台 | 检查项 |
| --- | --- |
| Windows 10/11 | 双击 bat 打开 GUI；下载 1 GB+ 文件；下载中拔网线 30 秒后恢复；断点续传；`--parts` |
| macOS | `python run_cli.py` 下载与续传；GUI 能打开 |
| Linux | 同上；`fcntl` 文件锁正常 |
| 全部 | `pip install .` 后 `ghdl-dl --version`、`ghdl-dl-gui` 可用 |
