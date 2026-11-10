# SPDX-License-Identifier: MIT
"""图形界面（tkinter）：任务列表、进度、暂停/续传、自动重试。

界面只做三件事：收集参数、把 ``DownloadJob`` 放到后台线程跑、定时刷新进度。
真正的下载/续传逻辑都在 engine 里，命令行与图形界面共用同一套引擎。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import __version__
from .engine import DownloadJob, Result, Snapshot
from .model import human_eta, human_size, write_json_atomic

APP_NAME = "ghdl"
IS_WINDOWS = os.name == "nt"


def _asset_path(name: str) -> Path | None:
    path = Path(__file__).resolve().parent / "assets" / name
    return path if path.exists() else None


def _config_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


CONFIG_DIR = _config_dir()
TASKS_FILE = CONFIG_DIR / "tasks.json"


@dataclass
class Task:
    id: str
    url: str
    output_dir: str
    connections: int = 8
    chunk_size: int | None = None
    sha256: str | None = None
    part_mode: bool = False
    proxy: str | None = None
    status: str = "queued"  # queued/running/pausing/paused/done/error/skipped
    message: str = ""
    output: str = ""
    created: float = field(default_factory=time.time)
    job: DownloadJob | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    snapshot: Snapshot | None = field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "output_dir": self.output_dir,
            "connections": self.connections,
            "chunk_size": self.chunk_size,
            "sha256": self.sha256,
            "part_mode": self.part_mode,
            "proxy": self.proxy,
            "status": "paused" if self.status in ("running", "pausing", "queued") else self.status,
            "message": self.message,
            "output": self.output,
            "created": self.created,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Task":
        status = str(data.get("status") or "paused")
        if status in ("running", "pausing", "queued"):
            status = "paused"
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            url=str(data.get("url") or ""),
            output_dir=str(data.get("output_dir") or ""),
            connections=int(data.get("connections") or 8),
            chunk_size=int(data["chunk_size"]) if data.get("chunk_size") else None,
            sha256=data.get("sha256") or None,
            part_mode=bool(data.get("part_mode")),
            proxy=data.get("proxy") or None,
            status=status,
            message=str(data.get("message") or ""),
            output=str(data.get("output") or ""),
            created=float(data.get("created") or time.time()),
        )


class TaskCard(ttk.Frame):
    """一个任务卡片。"""

    def __init__(self, master: tk.Misc, task: Task, app: "App") -> None:
        super().__init__(master, padding=(12, 8, 12, 8), style="Card.TFrame")
        self.app = app
        self.task_id = task.id
        self.columnconfigure(0, weight=1)

        self.name_var = tk.StringVar(value=task.url)
        self.meta_var = tk.StringVar(value="等待开始")
        self.status_var = tk.StringVar(value="排队中")
        self.progress = ttk.Progressbar(self, maximum=1000, value=0)

        name = ttk.Label(self, textvariable=self.name_var, style="Name.TLabel", anchor="w")
        name.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.meta = ttk.Label(self, textvariable=self.meta_var, style="Muted.TLabel", anchor="e")
        self.meta.grid(row=2, column=1, sticky="e", padx=(16, 12))

        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 4))

        self.status = ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel", anchor="w")
        self.status.grid(row=2, column=0, sticky="w")

        buttons = ttk.Frame(self, style="Card.TFrame")
        buttons.grid(row=0, column=2, rowspan=3, sticky="e", padx=(8, 0))
        self.toggle_btn = ttk.Button(buttons, text="暂停", width=6, command=lambda: app.toggle_pause(task.id))
        self.toggle_btn.pack(side="left", padx=3)
        ttk.Button(buttons, text="目录", width=6, command=lambda: app.open_folder(task.id)).pack(side="left", padx=3)
        ttk.Button(buttons, text="移除", width=6, command=lambda: app.remove_task(task.id)).pack(side="left", padx=3)

        self.refresh(task)

    def refresh(self, task: Task) -> None:
        label = Path(task.output).name if task.output else task.url.rstrip("/").rsplit("/", 1)[-1]
        self.name_var.set(label or task.url)
        snap = task.snapshot
        total = snap.total if snap else 0
        done = snap.downloaded if snap else 0
        self.status.grid_configure(columnspan=2 if task.status == "error" else 1)

        if task.status == "done":
            self.progress["value"] = 1000
            self.meta_var.set(human_size(total) if total else "已完成")
            self.status_var.set("✔ 已完成")
            self.status.configure(style="Ok.TLabel")
            self.toggle_btn.state(["disabled"])
            return
        if task.status == "skipped":
            self.progress["value"] = 1000
            self.meta_var.set(human_size(total) if total else "")
            self.status_var.set("✔ 文件已存在")
            self.status.configure(style="Ok.TLabel")
            self.toggle_btn.state(["disabled"])
            return
        if task.status == "error":
            self.status_var.set("✘ " + (task.message or "下载失败"))
            self.status.configure(style="Danger.TLabel")
            self.toggle_btn.configure(text="重试")
            self.toggle_btn.state(["!disabled"])
            self.meta_var.set("")
            self.progress["value"] = int((done / total * 1000) if total else 0)
            return
        if task.status == "paused":
            self.status_var.set("⏸ 已暂停 · 进度已保存")
            self.status.configure(style="Warn.TLabel")
            self.toggle_btn.configure(text="继续")
            self.toggle_btn.state(["!disabled"])
            self.meta_var.set(f"{human_size(done)} / {human_size(total)}" if total else "")
            self.progress["value"] = int((done / total * 1000) if total else 0)
            return
        if task.status in ("running", "pausing"):
            waiting = bool(snap and snap.status == "waiting")
            self.status.configure(style="Warn.TLabel" if waiting else "Muted.TLabel")
            self.toggle_btn.configure(text="暂停")
            self.toggle_btn.state(["!disabled"])
            if snap and total:
                pct = snap.fraction * 100
                self.progress["value"] = int(snap.fraction * 1000)
                self.meta_var.set(
                    f"{human_size(done)} / {human_size(total)} · {human_size(snap.speed)}/s"
                    f" · ETA {human_eta(snap.eta)}"
                )
                if snap.status == "waiting":
                    detail = f"等待重连…（{snap.retries} 次）"
                else:
                    detail = f"{pct:.1f}% · 连接 {snap.active}/{snap.connections}"
                    if snap.retries:
                        detail += f" · 重试 {snap.retries}"
                if task.status == "pausing":
                    detail = "正在暂停…"
                self.status_var.set(detail)
            else:
                self.status_var.set(snap.message if snap else "正在连接…")
                self.meta_var.set("正在获取文件信息…")
            return
        self.status.configure(style="Muted.TLabel")
        self.status_var.set("排队中")
        self.toggle_btn.configure(text="暂停")
        self.meta_var.set("")


class App(tk.Tk):
    def __init__(self) -> None:
        _enable_dpi()
        super().__init__()
        self.title(f"ghdl-dl · GitHub 大文件分段下载器  v{__version__}")
        self.geometry("980x780")
        self.minsize(800, 600)
        self.configure(bg="#f5f6f8")

        self.tasks: list[Task] = []
        self.cards: dict[str, TaskCard] = {}
        self.events: queue.Queue[tuple] = queue.Queue()
        self.max_parallel = 2
        self._closing = False

        self._set_icon()
        self._init_style()
        self._build_ui()
        self._load_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._tick)
        self.after(400, self._maybe_autorun)

    # ------------------------------------------------------------------ 界面
    def _set_icon(self) -> None:
        png = _asset_path("icon.png")
        ico = _asset_path("icon.ico")
        try:
            if IS_WINDOWS and ico is not None:
                self.iconbitmap(str(ico))
                return
            if png is not None:
                self._icon_image = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_image)
        except (tk.TclError, OSError):
            pass

    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base = ("Microsoft YaHei UI", 9)
        try:
            import tkinter.font as tkfont

            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
        except Exception:
            pass
        style.configure(".", font=base)
        style.configure("TFrame", background="#f5f6f8")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1,
                        bordercolor="#e2e5ea")
        style.configure("TLabel", background="#f5f6f8", foreground="#1f2937")
        style.configure("Name.TLabel", background="#ffffff", font=(base[0], 10, "bold"), foreground="#111827")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6b7280")
        style.configure("Ok.TLabel", background="#ffffff", foreground="#15803d")
        style.configure("Warn.TLabel", background="#ffffff", foreground="#b45309")
        style.configure("Danger.TLabel", background="#ffffff", foreground="#b91c1c")
        style.configure("Title.TLabel", font=(base[0], 14, "bold"), foreground="#0f172a")
        style.configure("Sub.TLabel", foreground="#6b7280")
        style.configure("Accent.TButton", font=(base[0], 9, "bold"))
        style.configure("TProgressbar", thickness=9, troughcolor="#eef1f5",
                        background="#3b82f6", bordercolor="#e5e7eb", lightcolor="#3b82f6",
                        darkcolor="#3b82f6")
        style.configure("TLabelframe", background="#f5f6f8")
        style.configure("TLabelframe.Label", background="#f5f6f8", foreground="#374151")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # 标题
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="GitHub 大文件下载器", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="多连接分段 · 断点续传 · 断网自动重连 · SHA-256 校验",
            style="Sub.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        # 表单
        form = ttk.LabelFrame(outer, text=" 新建下载 ", padding=(10, 8))
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="链接").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(form, textvariable=self.url_var)
        entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 8))
        entry.bind("<Return>", lambda _e: self.add_from_form())
        ttk.Button(form, text="添加任务", style="Accent.TButton", command=self.add_from_form).grid(row=0, column=4)

        ttk.Label(form, text="保存到").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.dir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        ttk.Entry(form, textvariable=self.dir_var).grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(form, text="浏览…", command=self._choose_dir).grid(row=1, column=2, pady=(8, 0))
        ttk.Label(form, text="连接数").grid(row=1, column=3, sticky="e", pady=(8, 0))
        self.conn_var = tk.IntVar(value=8)
        ttk.Spinbox(form, from_=1, to=32, width=4, textvariable=self.conn_var).grid(
            row=1, column=4, sticky="w", padx=(6, 0), pady=(8, 0)
        )

        self.advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form, text="高级选项", variable=self.advanced_var, command=self._toggle_advanced
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        self.adv = ttk.Frame(form)
        self.adv.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        for col in (1, 3):
            self.adv.columnconfigure(col, weight=1)
        ttk.Label(self.adv, text="Token").grid(row=0, column=0, sticky="w")
        self.token_var = tk.StringVar(value=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
        ttk.Entry(self.adv, textvariable=self.token_var, show="•").grid(
            row=0, column=1, sticky="ew", padx=(6, 12)
        )
        ttk.Label(self.adv, text="SHA-256").grid(row=0, column=2, sticky="w")
        self.sha_var = tk.StringVar()
        ttk.Entry(self.adv, textvariable=self.sha_var).grid(row=0, column=3, sticky="ew", padx=(6, 0))

        ttk.Label(self.adv, text="代理").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.proxy_var = tk.StringVar(value=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
        ttk.Entry(self.adv, textvariable=self.proxy_var).grid(
            row=1, column=1, sticky="ew", padx=(6, 12), pady=(6, 0)
        )
        self.parts_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.adv, text="每段单独文件，完成后拼接", variable=self.parts_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Label(self.adv, text="分段大小").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.chunk_var = tk.StringVar()
        ttk.Entry(self.adv, textvariable=self.chunk_var, width=10).grid(
            row=2, column=1, sticky="w", padx=(6, 12), pady=(6, 0)
        )
        ttk.Label(
            self.adv, text="留空自动（建议 8M–32M）；Token 用于私有仓库/避免限流", style="Sub.TLabel"
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self.adv.grid_remove()

        # 工具栏
        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, sticky="new", pady=(12, 6))
        ttk.Label(bar, text="任务", style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="全部开始", command=self.start_all).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="全部暂停", command=self.pause_all).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="打开下载目录", command=self._open_download_dir).pack(side="right", padx=(6, 0))
        ttk.Label(bar, text="同时任务", style="Sub.TLabel").pack(side="right", padx=(12, 6))
        self.parallel_var = tk.IntVar(value=2)
        ttk.Spinbox(bar, from_=1, to=6, width=3, textvariable=self.parallel_var,
                    command=self._read_parallel).pack(side="right")
        self.parallel_var.trace_add("write", lambda *_: self._read_parallel())

        # 任务列表
        list_frame = ttk.Frame(outer)
        list_frame.grid(row=3, column=0, sticky="nsew")
        self.canvas = tk.Canvas(list_frame, highlightthickness=0, bg="#f5f6f8")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas)
        self.inner_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self.empty_label = ttk.Label(
            self.inner, text="还没有任务。粘贴 GitHub Release 资源链接，点击“添加任务”。",
            style="Sub.TLabel", padding=24,
        )
        self.empty_label.pack(fill="x")

        # 日志
        log_frame = ttk.LabelFrame(outer, text=" 日志 ", padding=(8, 6))
        log_frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=4, wrap="none", relief="flat",
                                bg="#fbfbfd", fg="#374151", insertbackground="#374151")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(outer, textvariable=self.status_var, style="Sub.TLabel").grid(
            row=5, column=0, sticky="w", pady=(8, 0)
        )
        self.outer = outer

        if not self.dir_var.get():
            self.dir_var.set(str(Path.home() / "Downloads"))

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.inner_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        widget = event.widget
        try:
            if isinstance(widget, tk.Text) or str(widget).startswith(str(self.log_text)):
                return
        except Exception:
            pass
        if self.canvas.winfo_exists():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _toggle_advanced(self) -> None:
        if self.advanced_var.get():
            self.adv.grid()
        else:
            self.adv.grid_remove()

    def _choose_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if path:
            self.dir_var.set(path)

    # ------------------------------------------------------------------ 任务
    def _read_parallel(self) -> None:
        try:
            self.max_parallel = max(1, min(6, int(self.parallel_var.get())))
        except (tk.TclError, ValueError):
            self.max_parallel = 2

    def _connections(self) -> int:
        try:
            value = int(self.conn_var.get())
        except (tk.TclError, ValueError):
            value = 8
        return max(1, min(32, value))

    def add_from_form(self) -> None:
        raw = self.url_var.get().strip()
        if not raw:
            messagebox.showinfo("提示", "请先粘贴下载链接。")
            return
        urls = [line.strip() for line in raw.replace("\r", "\n").split("\n") if line.strip()]
        if len(urls) == 1 and " " in urls[0]:
            urls = [u for u in urls[0].split(" ") if u]
        out_dir = self.dir_var.get().strip() or str(Path.home() / "Downloads")
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法创建目录", str(exc))
            return
        for url in urls:
            if not url.lower().startswith(("http://", "https://")):
                messagebox.showerror("链接无效", f"不是有效的 HTTP 链接：\n{url}")
                continue
            if any(t.url == url and t.output_dir == out_dir for t in self.tasks):
                continue
            task = Task(
                id=uuid.uuid4().hex,
                url=url,
                output_dir=out_dir,
                connections=self._connections(),
                chunk_size=_parse_chunk(self.chunk_var.get()),
                sha256=self.sha_var.get().strip() or None,
                part_mode=bool(self.parts_var.get()),
                proxy=self.proxy_var.get().strip() or None,
                status="queued",
            )
            self._add_task(task)
        self.url_var.set("")
        self._save_state()
        self._schedule()

    def _add_task(self, task: Task) -> None:
        self.tasks.append(task)
        card = TaskCard(self.inner, task, self)
        card.pack(fill="x", pady=4, padx=2)
        self.cards[task.id] = card
        self._update_empty()

    def _update_empty(self) -> None:
        if self.tasks:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(fill="x")

    def _find(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def _schedule(self) -> None:
        running = sum(1 for t in self.tasks if t.status in ("running", "pausing"))
        for task in self.tasks:
            if running >= self.max_parallel:
                break
            if task.status == "queued":
                self._start(task)
                running += 1

    def _start(self, task: Task) -> None:
        if task.status in ("running", "pausing"):
            return
        try:
            job = DownloadJob(
                task.url,
                task.output_dir + ("" if task.output_dir.endswith(os.sep) else os.sep),
                connections=task.connections,
                chunk_size=task.chunk_size,
                token=self.token_var.get().strip() or None,
                sha256=task.sha256,
                proxy=task.proxy,
                part_mode=task.part_mode,
                log=lambda m, tid=task.id: self.events.put(("log", tid, m)),
            )
        except (ValueError, OSError) as exc:
            task.status = "error"
            task.message = str(exc)
            self._refresh_card(task)
            self._append_log("✘ " + task.url + "\n  " + str(exc))
            return
        task.job = job
        task.status = "running"
        task.message = ""
        task.snapshot = None

        def run() -> None:
            try:
                result = job.run()
            except Exception as exc:  # pragma: no cover
                result = Result("error", error=f"{type(exc).__name__}: {exc}")
            self.events.put(("finished", task.id, result))

        task.thread = threading.Thread(target=run, name=f"task-{task.id[:6]}", daemon=True)
        task.thread.start()
        self._refresh_card(task)
        self._save_state()

    def toggle_pause(self, task_id: str) -> None:
        task = self._find(task_id)
        if task is None:
            return
        if task.status in ("running", "pausing"):
            task.status = "pausing"
            if task.job:
                task.job.request_stop()
            self._refresh_card(task)
            return
        if task.status in ("paused", "error", "skipped", "queued"):
            task.status = "queued"
            self._refresh_card(task)
            self._schedule()

    def pause_all(self) -> None:
        for task in self.tasks:
            if task.status in ("running", "pausing"):
                task.status = "pausing"
                if task.job:
                    task.job.request_stop()
                self._refresh_card(task)

    def start_all(self) -> None:
        for task in self.tasks:
            if task.status in ("paused", "error", "queued"):
                task.status = "queued"
                self._refresh_card(task)
        self._schedule()

    def remove_task(self, task_id: str) -> None:
        task = self._find(task_id)
        if task is None:
            return
        if task.status in ("running", "pausing"):
            if not messagebox.askyesno("移除任务", "任务正在下载，移除将暂停并保留已下载的进度。继续？"):
                return
            if task.job:
                task.job.request_stop()
        self.tasks.remove(task)
        card = self.cards.pop(task_id, None)
        if card:
            card.destroy()
        self._update_empty()
        self._save_state()
        self._schedule()

    def open_folder(self, task_id: str) -> None:
        task = self._find(task_id)
        if task is None:
            return
        target = Path(task.output) if task.output else Path(task.output_dir)
        folder = target.parent if target.suffix else target
        if not folder.exists():
            folder = Path(task.output_dir)
        try:
            if IS_WINDOWS:
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _open_download_dir(self) -> None:
        path = Path(self.dir_var.get() or str(Path.home()))
        path.mkdir(parents=True, exist_ok=True)
        try:
            if IS_WINDOWS:
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc))

    # ------------------------------------------------------------------ 事件循环
    def _tick(self) -> None:
        if self._closing:
            return
        self._drain_events()
        total_speed = 0.0
        active = 0
        for task in self.tasks:
            if task.status == "running" and task.job is not None:
                task.snapshot = task.job.snapshot()
                total_speed += task.snapshot.speed
                active += 1
                self._refresh_card(task)
        if active:
            self.status_var.set(
                f"{active} 个任务下载中 · 总速度 {human_size(total_speed)}/s · "
                f"并发上限 {self.max_parallel}"
            )
        else:
            done = sum(1 for t in self.tasks if t.status in ("done", "skipped"))
            self.status_var.set(f"空闲 · 共 {len(self.tasks)} 个任务，已完成 {done} 个")
        self.after(250, self._tick)

    def _drain_events(self) -> None:
        while True:
            try:
                kind, task_id, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                self._append_log(f"[{task_id[:6]}] {payload}")
                continue
            if kind == "finished":
                self._on_finished(task_id, payload)

    def _on_finished(self, task_id: str, result: Result) -> None:
        task = self._find(task_id)
        if task is None:
            return
        task.job = None
        task.thread = None
        task.output = str(result.output or task.output)
        task.status = result.status
        task.message = result.error or ""
        if result.status == "done":
            task.snapshot = Snapshot(status="done", total=result.total, downloaded=result.total)
            self._append_log(f"✔ 完成 {task.output}（{human_size(result.total)}，{result.seconds:.0f}s）")
        elif result.status == "skipped":
            task.snapshot = Snapshot(status="skipped", total=result.total, downloaded=result.total)
            self._append_log(f"✔ 已存在，跳过 {task.output}")
        elif result.status == "paused":
            self._append_log(f"⏸ 已暂停 {task.url}")
        else:
            self._append_log(f"✘ 失败 {task.url}\n  {task.message}")
        self._refresh_card(task)
        self._save_state()
        self._schedule()

    def _refresh_card(self, task: Task) -> None:
        card = self.cards.get(task.id)
        if card is not None:
            card.refresh(task)

    def _append_log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        for line in str(message).splitlines():
            self.log_text.insert("end", f"[{stamp}] {line}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.delete("1.0", f"{lines - 2000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------ 持久化
    def _load_state(self) -> None:
        try:
            data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        last_dir = data.get("last_dir")
        if isinstance(last_dir, str) and last_dir:
            self.dir_var.set(last_dir)
        conn = data.get("connections")
        if isinstance(conn, int) and 1 <= conn <= 32:
            self.conn_var.set(conn)
        self.max_parallel = int(data.get("max_parallel") or 2)
        self.parallel_var.set(self.max_parallel)
        self.advanced_var.set(bool(data.get("advanced")))
        self._toggle_advanced()
        for entry in data.get("tasks") or []:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            try:
                task = Task.from_json(entry)
            except (TypeError, ValueError):
                continue
            self._add_task(task)

    def _save_state(self) -> None:
        data = {
            "version": 1,
            "last_dir": self.dir_var.get(),
            "connections": self._connections(),
            "max_parallel": self.max_parallel,
            "advanced": bool(self.advanced_var.get()),
            "tasks": [t.to_json() for t in self.tasks],
        }
        try:
            write_json_atomic(TASKS_FILE, data)
        except OSError:
            pass

    def _maybe_autorun(self) -> None:
        # 上次退出时没下完的任务：自动排队继续（状态文件在磁盘上，可续传）
        pending = [t for t in self.tasks if t.status in ("paused", "queued")]
        if pending and not self._closing:
            self._append_log(f"发现 {len(pending)} 个未完成任务，自动继续")
            for task in pending:
                task.status = "queued"
            self._schedule()

    # ------------------------------------------------------------------ 退出
    def _on_close(self) -> None:
        running = [t for t in self.tasks if t.status in ("running", "pausing")]
        if running and not messagebox.askyesno(
            "退出", f"还有 {len(running)} 个任务正在下载。\n暂停并保存进度后退出？"
        ):
            return
        self._closing = True
        for task in running:
            if task.job:
                task.job.request_stop()
        deadline = time.monotonic() + 8
        for task in running:
            if task.thread is not None:
                task.thread.join(max(0.2, deadline - time.monotonic()))
        self._save_state()
        self.destroy()


def _parse_chunk(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    from .cli import parse_size

    try:
        return parse_size(text)
    except Exception:
        return None


def _enable_dpi() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _set_app_id() -> None:
    """让 Windows 任务栏把窗口归到 ghdl-dl，而不是 python.exe。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("c4h2o.ghdl-dl")
    except Exception:
        pass


def main() -> int:
    _set_app_id()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
