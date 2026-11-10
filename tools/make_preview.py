# SPDX-License-Identifier: MIT
"""生成 GUI 界面预览图 ``gui_preview.png``（用于 README 等）。

用法::

    python tools/make_preview.py

会启动一个真实的 tkinter 窗口（短暂闪现），注入 4 个演示任务
（下载中 / 已暂停 / 已完成 / 失败），更新一次界面后用 ``ImageGrab``
截取窗口区域（含标题栏，能显示应用图标），保存到仓库根目录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ghdl.gui import App, Task  # noqa: E402
from ghdl.engine import Snapshot  # noqa: E402

TOTAL = 2_147_483_648  # 2 GiB
SAVE_DIR = r"F:\Downloads"


def _demo_tasks() -> list[Task]:
    running = Task(
        id="demo-running", url="https://github.com/c4h2o/demo/releases/download/v1/dataset.7z",
        output_dir=SAVE_DIR, status="running",
        output=SAVE_DIR + r"\dataset.7z",
    )
    running.snapshot = Snapshot(
        status="idle", total=TOTAL, downloaded=int(TOTAL * 0.637),
        speed=41.5 * 1024 * 1024, eta=14.9, retries=2, connections=8, active=8,
    )

    paused = Task(
        id="demo-paused", url="https://github.com/c4h2o/demo/releases/download/v1/assets.tar.xz",
        output_dir=SAVE_DIR, status="paused",
        output=SAVE_DIR + r"\assets.tar.xz",
    )
    paused.snapshot = Snapshot(
        status="idle", total=3_221_225_472, downloaded=1_208_959_488,
        connections=8, active=0,
    )

    done = Task(
        id="demo-done", url="https://github.com/c4h2o/demo/releases/download/v1/models.zip",
        output_dir=SAVE_DIR, status="done",
        output=SAVE_DIR + r"\models.zip",
    )
    done.snapshot = Snapshot(status="done", total=1_073_741_824, downloaded=1_073_741_824)

    failed = Task(
        id="demo-failed", url="https://github.com/c4h2o/demo/releases/download/v1/weights.bin",
        output_dir=SAVE_DIR, status="error",
        message="服务器拒绝分段请求（HTTP 403）",
        output=SAVE_DIR + r"\weights.bin",
    )
    failed.snapshot = Snapshot(
        status="idle", total=2_684_354_560, downloaded=536_870_912,
    )

    return [running, paused, done, failed]


def main() -> int:
    # 用临时状态文件，避免加载真实任务 / 触发自动续传
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPDATA"] = tmp
        import ghdl.gui as gui

        gui.TASKS_FILE = Path(tmp) / "tasks.json"
        # 预览不自动续传（否则会启动真实任务连网）。必须在构造 App 前打补丁，
        # 因为 __init__ 里 after(400, self._maybe_autorun) 绑定的是原方法。
        gui.App._maybe_autorun = lambda self: None  # type: ignore[method-assign]

        app = App()
        app.geometry("980x940+60+10")
        app.title("ghdl-dl · GitHub 大文件分段下载器  v1.0.0")
        app.dir_var.set(SAVE_DIR)  # “保存到”字段显示 F:\Downloads
        app.attributes("-topmost", True)
        app.deiconify()
        app.lift()
        app.after(150, lambda: app.attributes("-topmost", False))

        for task in _demo_tasks():
            app._add_task(task)
        app._update_empty()
        app.update_idletasks()

        # 日志里补两行，更真实
        app._append_log("✔ 完成 dataset.7z（2.0 GiB，184s）")
        app._append_log("⏸ 已暂停 assets.tar.xz，进度已保存")

        app.update()
        time.sleep(0.35)
        app.update()

        # 用客户端矩形 + 标题栏偏移截取：窗口完全落在屏幕内，避免裁切/露桌面
        from PIL import ImageGrab

        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        top = 32  # 标题栏高度（含窗口标题），用于显示图标
        img = ImageGrab.grab(bbox=(x, y - top, x + w, y + h))
        out = ROOT / "gui_preview.png"
        if out.exists():
            out.unlink()  # 可能被外部查看器占用，先删再写
        img.save(out, optimize=True)
        print(f"saved {out}  ({img.size[0]}x{img.size[1]}, {out.stat().st_size} bytes)")

        app.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
