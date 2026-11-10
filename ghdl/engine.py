# SPDX-License-Identifier: MIT
"""下载引擎：多连接分段、断点续传、断线自动重连、校验、组装。

设计要点
--------
* **分段**：把文件按 ``chunk`` 切开放进队列，N 个线程各领一段并行下载，
  先下完的线程继续领下一段（动态负载均衡，不会出现“7 个线程等 1 个慢段”）。
* **落盘**：默认为一个预分配的 ``.ghdlpart`` 文件，各线程按偏移随机写入，
  下载完成时文件本身就是拼好的，不需要再读一遍写一遍。``--parts`` 模式则
  每段一个文件，最后按序拼接到目标文件。
* **续传**：每段的进度写入 ``.ghdlpart.json``（原子替换 + fsync），
  进程被杀、断网、重启后重新运行即可从断点继续。
* **重连**：连接层错误一律重试（默认无限次、指数退避），并在状态里提示
  “等待网络恢复”。GitHub 的 CDN 地址是带签名的、会过期，遇到 403 会自动
  重新解析原始链接换取新地址。
"""

from __future__ import annotations

import errno
import hashlib
import os
import queue
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .http_client import HttpClient, HttpStatusError, NetworkError, Response, TlsCertificateError
from .model import (
    Chunk,
    FileLock,
    JobState,
    LockBusyError,
    filename_from_url,
    human_size,
    load_state,
    plan_chunks,
    sanitize_filename,
    write_json_atomic,
)
from .probe import Probe, github_release_digest, parse_content_range, probe as probe_url
from .writer import PartsWriter, SingleFileWriter, StreamWriter, Writer

BLOCK = 256 * 1024
SPEED_WINDOW = 5.0

#: 这些错误重试也没用，直接报错（磁盘满、无权限、只读等）
_FATAL_ERRNOS = {
    errno.ENOSPC,
    getattr(errno, "EDQUOT", errno.ENOSPC),
    errno.EACCES,
    errno.EPERM,
    errno.EROFS,
    errno.ENAMETOOLONG,
}


def _is_fatal_os_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in _FATAL_ERRNOS


class _Stopped(Exception):
    """用户暂停 / Ctrl+C。"""


class FatalError(Exception):
    """无法自动恢复的错误。"""


@dataclass
class Snapshot:
    status: str = "idle"
    message: str = ""
    total: int = 0
    downloaded: int = 0
    speed: float = 0.0
    eta: float | None = None
    retries: int = 0
    connections: int = 0
    active: int = 0
    workers: list[tuple[int, int, int]] = field(default_factory=list)
    error: str | None = None
    output: str = ""
    elapsed: float = 0.0
    ranges: bool = True
    part_mode: bool = False
    chunk_total: int = 0
    chunk_done: int = 0

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.downloaded / self.total))


@dataclass
class Result:
    status: str  # done / skipped / paused / error
    output: Path | None = None
    downloaded: int = 0
    total: int = 0
    seconds: float = 0.0
    retries: int = 0
    error: str | None = None


class UrlResolver:
    """管理 GitHub 原始链接与签名 CDN 地址之间的解析/刷新。"""

    def __init__(self, client: HttpClient, url: str, first: Probe) -> None:
        self._client = client
        self._url = url
        self._probe = first
        self._gen = 1
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._refreshing = False

    @property
    def probe(self) -> Probe:
        return self._probe

    def current(self) -> tuple[str, int]:
        with self._lock:
            return self._probe.final_url, self._gen

    def refresh(self, stale_gen: int) -> bool:
        """重新解析链接。返回 True 表示已有新地址；False 表示别的线程刚试过仍失败。"""
        with self._lock:
            if self._gen != stale_gen:
                return True
            if self._refreshing:
                self._cond.wait(timeout=120.0)
                return self._gen != stale_gen
            self._refreshing = True
        try:
            fresh = probe_url(self._client, self._url)
        finally:
            with self._lock:
                self._refreshing = False
                self._cond.notify_all()
        with self._lock:
            if fresh.size is not None and self._probe.size is not None and fresh.size != self._probe.size:
                raise FatalError("远端文件大小发生变化，无法继续当前任务")
            self._probe = fresh
            self._gen += 1
        return True


class DownloadJob:
    """一次（可续传的）下载任务。"""

    def __init__(
        self,
        url: str,
        output: str | Path,
        *,
        connections: int = 8,
        chunk_size: int | None = None,
        token: str | None = None,
        sha256: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 0,
        retry_base: float = 1.0,
        retry_max: float = 30.0,
        proxy: str | None = None,
        resume: bool = True,
        restart: bool = False,
        part_mode: bool = False,
        api_digest: bool = True,
        insecure: bool = False,
        force: bool = False,
        min_chunk: int = 2 * 1024 * 1024,
        max_chunk: int = 64 * 1024 * 1024,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.url = (url or "").strip()
        if not self.url:
            raise ValueError("URL 不能为空")
        self._output_arg = str(output)
        self._output_is_dir = self._looks_like_dir(self._output_arg)
        self.output = Path(self._output_arg)
        self.connections = max(1, min(64, int(connections)))
        self.chunk_size = int(chunk_size) if chunk_size else None
        self.token = token
        self.sha256 = self._normalize_sha(sha256)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.retry_base = max(0.05, float(retry_base))
        self.retry_max = max(self.retry_base, float(retry_max))
        self.proxy = proxy
        self.resume = bool(resume)
        self.restart = bool(restart)
        self.part_mode = bool(part_mode)
        self.api_digest = bool(api_digest)
        self.insecure = bool(insecure)
        self.force = bool(force)
        self.min_chunk = int(min_chunk)
        self.max_chunk = int(max_chunk)
        self._log_fn = log

        self._client = HttpClient(
            token=self.token, timeout=self.timeout, proxy=self.proxy,
            insecure=self.insecure,
        )
        self._api_client = HttpClient(
            token=self.token, timeout=min(self.timeout, 15.0), proxy=self.proxy,
            insecure=self.insecure,
        )

        self.part_path = Path()
        self.state_path = Path()
        self.lock_path = Path()
        self.parts_dir = Path()
        self.merge_path = Path()
        self._refresh_paths()

        # 运行时状态
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._state: JobState | None = None
        self._writer: Writer | None = None
        self._lock_file: FileLock | None = None
        self._resolver: UrlResolver | None = None
        self._probe: Probe | None = None
        self._fatal: FatalError | None = None
        self._status = "idle"
        self._message = ""
        self._error: str | None = None
        self._downloaded = 0
        self._run_bytes = 0
        self._retries = 0
        self._t0 = 0.0
        self._samples: list[tuple[float, int]] = []
        self._workers: dict[int, tuple[int, int, int]] = {}
        self._threads: list[threading.Thread] = []
        self._active: set[Response] = set()
        self._saver: threading.Thread | None = None
        self._saver_stop = threading.Event()
        self._queue: queue.Queue[Chunk] = queue.Queue()

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _looks_like_dir(value: str) -> bool:
        if value.endswith(("/", "\\")):
            return True
        try:
            return Path(value).expanduser().is_dir()
        except OSError:
            return False

    @staticmethod
    def _normalize_sha(value: str | None) -> str | None:
        if not value:
            return None
        text = value.strip().lower()
        if text.startswith("sha256:"):
            text = text.split(":", 1)[1].strip()
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError("SHA-256 格式不正确（应为 64 位十六进制）")
        return text

    def _refresh_paths(self) -> None:
        out = Path(self.output)
        base = out.name
        if not base:
            out = out / "download.bin"
            base = out.name
        self.output = out
        self.part_path = out.with_name(base + ".ghdlpart")
        self.state_path = out.with_name(base + ".ghdlpart.json")
        self.lock_path = out.with_name(base + ".ghdlpart.lock")
        self.parts_dir = out.with_name(base + ".ghdlparts")
        self.merge_path = out.with_name(base + ".ghdlmerge")

    def _log(self, message: str) -> None:
        if self._log_fn:
            try:
                self._log_fn(message)
            except Exception:
                pass

    def _set_status(self, status: str, message: str = "") -> None:
        with self._lock:
            self._status = status
            if message:
                self._message = message

    def _add_bytes(self, n: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._downloaded += n
            self._run_bytes += n
            self._samples.append((now, n))
            if len(self._samples) > 4000:
                cutoff = now - SPEED_WINDOW
                self._samples = [s for s in self._samples if s[0] >= cutoff]

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> JobState | None:
        with self._lock:
            return self._state

    def snapshot(self) -> Snapshot:
        now = time.monotonic()
        with self._lock:
            st = self._state
            total = st.size if st else 0
            samples = [s for s in self._samples if s[0] >= now - SPEED_WINDOW]
            span = min(SPEED_WINDOW, max(0.2, now - self._t0)) if self._t0 else 1.0
            speed = sum(n for _t, n in samples) / span
            done = self._downloaded
            eta = (total - done) / speed if speed > 1 and total > done else None
            workers = sorted(self._workers.values()) if self._workers else []
            chunk_total = len(st.chunks) if st else 0
            chunk_done = sum(1 for c in st.chunks if c.finished) if st else 0
            return Snapshot(
                status=self._status,
                message=self._message,
                total=total,
                downloaded=done,
                speed=speed,
                eta=eta,
                retries=self._retries,
                connections=self.connections,
                active=len(workers),
                workers=workers,
                error=self._error,
                output=str(self.output),
                elapsed=now - self._t0 if self._t0 else 0.0,
                ranges=st.ranges if st else True,
                part_mode=bool(st.part_mode) if st else self.part_mode,
                chunk_total=chunk_total,
                chunk_done=chunk_done,
            )

    # ------------------------------------------------------------------ 控制
    def request_stop(self) -> None:
        """请求暂停：工作线程会在当前数据块结束后退出，进度写入磁盘。"""
        self._stop.set()
        for resp in self._active_responses():
            try:
                resp.abort()
            except Exception:
                pass

    # ------------------------------------------------------------------ 主流程
    def run(self) -> Result:
        self._reset_runtime()
        try:
            return self._run()
        except _Stopped:
            self._save_state(fsync=True)
            self._set_status("paused", "已暂停，进度已保存")
            return Result("paused", self.output, self._run_bytes,
                          self._state.size if self._state else 0,
                          time.monotonic() - self._t0, self._retries, "已暂停")
        except FatalError as exc:
            self._error = str(exc)
            self._set_status("error", str(exc))
            self._save_state(fsync=True)
            self._log(f"错误：{exc}")
            return Result("error", self.output, self._run_bytes,
                          self._state.size if self._state else 0,
                          time.monotonic() - self._t0, self._retries, str(exc))
        except Exception as exc:  # 兜底，避免 GUI 线程直接崩掉
            self._error = f"{type(exc).__name__}: {exc}"
            self._set_status("error", self._error)
            self._log("未预期的异常：\n" + traceback.format_exc())
            self._save_state(fsync=True)
            return Result("error", self.output, self._run_bytes,
                          self._state.size if self._state else 0,
                          time.monotonic() - self._t0, self._retries, self._error)
        finally:
            self._shutdown_runtime()

    def _reset_runtime(self) -> None:
        self._stop.clear()
        self._fatal = None
        self._error = None
        self._retries = 0
        self._run_bytes = 0
        self._downloaded = 0
        self._samples = []
        self._workers = {}
        self._threads = []
        self._active = set()
        self._t0 = time.monotonic()
        self._status = "starting"
        self._message = ""

    # -------------------------------------------------------------- 核心步骤
    def _run(self) -> Result:
        self._set_status("probing", "正在连接服务器…")
        probe = self._call_with_retry("连接服务器", lambda: probe_url(self._client, self.url))
        self._probe = probe
        self._resolver = UrlResolver(self._client, self.url, probe)
        self._finalize_output(probe)
        self._acquire_lock()
        if self._client.proxy:
            self._log(f"代理：{self._client.proxy[0]}:{self._client.proxy[1]}")
        self._log(
            f"服务器：{probe.final_url.split('?')[0][:120]}\n"
            f"大小：{human_size(probe.size)}    "
            f"分段下载：{'支持' if probe.ranges else '不支持（单流模式）'}"
        )

        expected = self.sha256
        if not expected and self.api_digest:
            sha, api_size, asset = github_release_digest(self._api_client, self.url)
            if sha:
                expected = sha
                self._log(f"已从 GitHub API 获取官方 SHA-256：{sha[:16]}…")
            if api_size is not None and probe.size is not None and api_size != probe.size:
                self._log(f"注意：API 报告大小 {human_size(api_size)} 与服务器返回 {human_size(probe.size)} 不一致")

        # 目标文件已存在？
        skip = self._check_existing(probe, expected)
        if skip is not None:
            return skip

        state = None if (self.restart or self.force or not self.resume) else load_state(self.state_path)
        if state is not None:
            reason = self._state_conflict(state, probe)
            if reason:
                if self.restart:
                    state = None
                else:
                    raise FatalError(f"无法续传（{reason}）。请加 --restart 重新下载，或删除 {self.state_path.name}")
        if state is None:
            state = self._new_state(probe, expected)
            self._discard_partials()
        else:
            if state.complete:
                self._log("数据已下载完，正在校验并收尾…")
            else:
                self._log(
                    f"发现未完成任务：已下载 {human_size(state.downloaded)} / {human_size(state.size)}"
                    f"（{state.downloaded / state.size * 100 if state.size else 0:.1f}%），继续…"
                )
        if expected and not state.sha256:
            state.sha256 = expected
        self._state = state
        self._downloaded = state.downloaded

        if state.ranges and state.size > 0 and not state.chunks:
            raise FatalError("任务文件损坏：没有任何分段")
        if not state.ranges:
            self._log("提示：该链接由服务器动态生成，不支持 Range，断线后只能从头下载。")

        if (state.ranges and not state.part_mode and state.downloaded > 0
                and not self.part_path.exists()):
            self._log("临时文件已丢失，无法续传，将从头下载")
            for chunk in state.chunks:
                chunk.done = 0
            self._downloaded = 0

        self._open_writer(state)
        if state.ranges:
            self._repair_progress(state)

        # 已完成？
        if state.complete and state.size > 0:
            return self._finish(state)

        self._set_status("downloading", "开始下载")
        self._start_saver()
        if state.ranges:
            self._run_chunks(state)
        else:
            self._run_stream(state)
        return self._finish_or_pause(state)

    def _finalize_output(self, probe: Probe) -> None:
        if self._output_is_dir:
            name = probe.filename or filename_from_url(probe.final_url or self.url)
            self.output = Path(self._output_arg).expanduser().absolute() / sanitize_filename(name)
        else:
            self.output = Path(self._output_arg).expanduser().absolute()
        self._refresh_paths()
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def _acquire_lock(self) -> None:
        lock = FileLock(self.lock_path)
        try:
            lock.acquire()
        except LockBusyError as exc:
            raise FatalError(
                f"{exc}。如果确认没有其它下载进程，请删除锁文件后重试：{self.lock_path}"
            ) from exc
        self._lock_file = lock

    def _check_existing(self, probe: Probe, expected: str | None = None) -> Result | None:
        """目标文件已存在且完整时直接跳过。"""
        if not self.output.exists() or self.force or self.restart:
            return None
        try:
            size = self.output.stat().st_size
        except OSError:
            return None
        expected = expected or self.sha256
        if probe.size is None and not expected:
            # 服务器没给大小，又无法校验哈希，不能断定已下载完整
            return None
        if probe.size is not None and size != probe.size:
            raise FatalError(
                f"目标文件已存在，但大小（{human_size(size)}）与远端（{human_size(probe.size)}）不同。"
                f"如需覆盖请加 --force"
            )
        if expected:
            self._set_status("verifying", "校验已存在的文件…")
            self._log("目标文件已存在，正在校验 SHA-256…")
            digest = self._sha256_file(self.output)
            if digest != expected:
                raise FatalError(
                    f"目标文件已存在，但 SHA-256 与期望值不符。如需重新下载请加 --force"
                )
        self._set_status("skipped", "文件已存在，无需下载")
        self._log(f"目标文件已存在且大小一致，跳过：{self.output}")
        self._cleanup_task_files(remove_state=True, remove_parts=True)
        return Result("skipped", self.output, 0, probe.size or size, 0.0, 0, None)

    def _state_conflict(self, state: JobState, probe: Probe) -> str | None:
        if state.url != self.url:
            return "链接与上次不同"
        if state.part_mode != self.part_mode:
            return "落盘模式（--parts）与上次不同"
        if probe.size is not None and state.size != probe.size:
            return f"远端大小从 {human_size(state.size)} 变为 {human_size(probe.size)}"
        if state.ranges and not probe.ranges:
            return "服务器不再支持 Range"
        if state.size > 0 and (not state.chunks or state.chunks[-1].end != state.size):
            return "任务文件中的分段信息不完整"
        return None

    def _new_state(self, probe: Probe, expected_sha: str | None) -> JobState:
        if probe.ranges and probe.size is not None:
            chunks = plan_chunks(
                probe.size, self.connections, self.chunk_size,
                min_chunk=self.min_chunk, max_chunk=self.max_chunk,
            )
            chunk_size = chunks[0].length if len(chunks) > 1 else probe.size
            self._log(
                f"计划：{self.connections} 个连接，共 {len(chunks)} 段，"
                f"每段约 {human_size(chunk_size)}"
            )
            return JobState(
                url=self.url, output=str(self.output), size=probe.size,
                chunk_size=chunk_size, ranges=True, chunks=chunks,
                etag=probe.etag, last_modified=probe.last_modified,
                sha256=expected_sha, part_mode=self.part_mode,
            )
        size = probe.size or 0
        self._log(f"单流模式：{'已知大小 ' + human_size(size) if size else '大小未知'}")
        return JobState(
            url=self.url, output=str(self.output), size=size,
            chunk_size=0, ranges=False, chunks=[Chunk(0, 0, size)],
            etag=probe.etag, last_modified=probe.last_modified,
            sha256=expected_sha, part_mode=False,
        )

    def _discard_partials(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
        _remove_file(self.part_path)
        _remove_tree(self.parts_dir)
        _remove_file(self.merge_path)

    def _open_writer(self, state: JobState) -> None:
        if not state.ranges:
            self._writer = StreamWriter(self.part_path)
        elif state.part_mode:
            self._writer = PartsWriter(self.parts_dir, state.size)
        else:
            self._writer = SingleFileWriter(self.part_path, state.size)

    def _repair_progress(self, state: JobState) -> None:
        """分段模式下，用磁盘上分片文件的实际大小修正状态，避免空洞写入。"""
        if not state.part_mode:
            return
        fixed = 0
        for chunk in state.chunks:
            path = self.parts_dir / f"part_{chunk.index:05d}.bin"
            actual = path.stat().st_size if path.exists() else 0
            if actual < chunk.done:
                chunk.done = max(0, min(actual, chunk.length))
                fixed += 1
        if fixed:
            self._log(f"检测到 {fixed} 个分片文件比记录的进度短，已回退这些分段")
            self._downloaded = state.downloaded
            self._save_state(fsync=True)

    # -------------------------------------------------------------- 分段下载
    def _run_chunks(self, state: JobState) -> None:
        todo = [c for c in state.chunks if not c.finished]
        if not todo:
            return
        for chunk in todo:
            self._queue.put(chunk)
        count = min(self.connections, len(todo))
        self._threads = [
            threading.Thread(target=self._worker, args=(i,), name=f"ghdl-{i}", daemon=True)
            for i in range(count)
        ]
        for t in self._threads:
            t.start()
        self._supervise()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            if not any(t.is_alive() for t in self._threads):
                break
            self._threads[0].join(0.2)
        for t in self._threads:
            t.join(1.0)
        if any(t.is_alive() for t in self._threads):
            self.request_stop()
            for t in self._threads:
                t.join(3.0)
        self._saver_stop.set()
        if self._saver:
            self._saver.join(3.0)
        self._save_state(fsync=True)
        if self._fatal:
            raise self._fatal

    def _worker(self, wid: int) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                return
            if chunk.finished:
                continue
            try:
                self._download_chunk(wid, chunk)
            except _Stopped:
                return
            except FatalError as exc:
                self._fail(exc)
                return
            except Exception as exc:  # pragma: no cover
                self._fail(FatalError(f"下载线程异常：{type(exc).__name__}: {exc}"))
                return
            finally:
                with self._lock:
                    self._workers.pop(wid, None)

    def _fail(self, exc: FatalError) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = exc
        self._stop.set()
        for resp in self._active_responses():
            try:
                resp.abort()
            except Exception:
                pass

    def _download_chunk(self, wid: int, chunk: Chunk) -> None:
        attempt = 0
        refresh_count = 0
        while not chunk.finished:
            if self._stop.is_set():
                raise _Stopped
            before = chunk.done
            try:
                self._fetch_once(wid, chunk)
                if chunk.done > before:
                    attempt = 0
                    refresh_count = 0
                if chunk.finished:
                    return
                raise NetworkError("连接提前结束，数据不完整")
            except _NeedRefresh as exc:
                if self._stop.is_set():
                    raise _Stopped from exc
                if chunk.done > before:
                    refresh_count = 0
                # 别的线程已经刷新过地址，直接用新地址重试即可
                if exc.gen and self._resolver.current()[1] != exc.gen:
                    continue
                if refresh_count >= 2:
                    raise FatalError(f"服务器拒绝分段请求（{exc}），无法继续") from exc
                self._log("CDN 地址已失效或忽略 Range，正在重新获取…")
                self._set_status("waiting", "签名地址过期，正在重新解析…")
                try:
                    self._resolver.refresh(exc.gen)
                    refresh_count += 1
                except FatalError:
                    raise
                except Exception as exc2:
                    attempt += 1
                    if not self._backoff(attempt, exc2, "刷新下载地址"):
                        raise _Stopped from exc2
                continue
            except HttpStatusError as exc:
                if self._stop.is_set():
                    raise _Stopped from exc
                if not exc.retryable:
                    raise FatalError(self._explain_http(exc)) from exc
                if chunk.done > before:
                    attempt = 0
                    refresh_count = 0
                    with self._lock:
                        self._retries += 1
                    self._set_status("downloading", "连接中断，正在续传…")
                    continue
                attempt += 1
                if not self._backoff(attempt, exc, "下载"):
                    raise _Stopped from exc
            except TlsCertificateError as exc:
                raise FatalError(self._explain_tls(exc)) from exc
            except (NetworkError, OSError) as exc:
                if self._stop.is_set():
                    raise _Stopped from exc
                if _is_fatal_os_error(exc):
                    raise FatalError(f"写入文件失败：{exc}") from exc
                if chunk.done > before:
                    # 已经有进展：立刻从断点续传，不做退避等待
                    attempt = 0
                    refresh_count = 0
                    with self._lock:
                        self._retries += 1
                    self._set_status("downloading", "连接中断，正在续传…")
                    continue
                attempt += 1
                if not self._backoff(attempt, exc, "下载"):
                    raise _Stopped from exc
            if chunk.done > before:
                attempt = 0
                refresh_count = 0

    def _fetch_once(self, wid: int, chunk: Chunk) -> None:
        assert self._resolver is not None and self._writer is not None
        url, gen = self._resolver.current()
        start = chunk.start + chunk.done
        end = chunk.end - 1
        try:
            resp = self._client.open(url, {"Range": f"bytes={start}-{end}"})
        except HttpStatusError as exc:
            # GitHub 的 CDN 地址带签名，过期后返回 403/404，需要重新解析原始链接
            if exc.status in (401, 403, 404, 410):
                raise _NeedRefresh(f"HTTP {exc.status}", gen) from exc
            raise
        self._register(resp, wid, chunk)
        try:
            if resp.status == 206:
                r_start, _r_end, total = parse_content_range(resp.header("content-range"))
                if total is not None and self._state is not None and total != self._state.size:
                    raise FatalError("远端文件大小发生变化，请重新开始任务")
                if r_start is not None and r_start != start:
                    raise _NeedRefresh(f"服务器返回的起点 {r_start} 与请求的 {start} 不一致", gen)
                remaining = chunk.end - start
                while remaining > 0:
                    if self._stop.is_set():
                        return
                    data = resp.read(min(BLOCK, remaining))
                    if not data:
                        break
                    self._writer.write(chunk, chunk.done, data)
                    with self._lock:
                        chunk.done += len(data)
                    self._add_bytes(len(data))
                    self._update_worker(wid, chunk)
                    remaining -= len(data)
                if remaining > 0 and not self._stop.is_set():
                    raise NetworkError(f"连接中断，本段还差 {human_size(remaining)}")
                return
            if resp.status == 200:
                # 服务器忽略了 Range（常见于动态生成的源码包）
                raise _NeedRefresh("服务器未按 Range 返回 206", gen)
            if resp.status == 416:
                if chunk.done >= chunk.length:
                    chunk.done = chunk.length
                    return
                raise FatalError("服务器返回 416（请求范围无效），请重新开始任务")
            raise HttpStatusError(resp.status, resp.url, resp.headers, b"", resp.reason)
        finally:
            self._unregister(resp)
            resp.close()

    def _active_responses(self) -> list[Response]:
        with self._lock:
            return list(self._active)

    def _register(self, resp: Response, wid: int, chunk: Chunk) -> None:
        with self._lock:
            self._active.add(resp)
            self._workers[wid] = (chunk.index, chunk.done, chunk.length)
        self._set_status("downloading", "下载中")

    def _unregister(self, resp: Response) -> None:
        with self._lock:
            self._active.discard(resp)

    def _update_worker(self, wid: int, chunk: Chunk) -> None:
        with self._lock:
            if wid in self._workers:
                idx, _done, length = self._workers[wid]
                self._workers[wid] = (idx, chunk.done, length)

    # -------------------------------------------------------------- 单流下载
    def _run_stream(self, state: JobState) -> None:
        assert self._writer is not None
        attempt = 0
        while True:
            if self._stop.is_set():
                return
            try:
                url, _gen = self._resolver.current() if self._resolver else (self.url, 0)
                resp = self._client.open(url, {})
                self._register(resp, 0, state.chunks[0])
                try:
                    if resp.status != 200:
                        raise HttpStatusError(resp.status, resp.url, resp.headers, b"", resp.reason)
                    writer = self._writer
                    assert isinstance(writer, StreamWriter)
                    writer.reset()
                    with self._lock:
                        state.chunks[0].done = 0
                    written = 0
                    while not self._stop.is_set():
                        data = resp.read(BLOCK)
                        if not data:
                            break
                        writer.write(data)
                        written += len(data)
                        with self._lock:
                            state.chunks[0].done = written
                            if state.size == 0:
                                state.size = written
                                state.chunks[0].end = written
                        self._add_bytes(len(data))
                        with self._lock:
                            self._workers[0] = (0, written, state.size or written)
                    if self._stop.is_set():
                        return
                    expected = resp.header_int("content-length")
                    if expected is not None and written != expected:
                        raise NetworkError(f"连接中断，已收到 {human_size(written)} / {human_size(expected)}")
                    self._log(f"单流下载完成，共 {human_size(written)}")
                    with self._lock:
                        state.size = written
                        state.chunks[0] = Chunk(0, 0, written, written)
                    return
                finally:
                    self._unregister(resp)
                    resp.close()
            except HttpStatusError as exc:
                if not exc.retryable:
                    raise FatalError(self._explain_http(exc)) from exc
                attempt += 1
                if not self._backoff(attempt, exc, "下载"):
                    raise _Stopped from exc
            except TlsCertificateError as exc:
                raise FatalError(self._explain_tls(exc)) from exc
            except (NetworkError, OSError) as exc:
                if _is_fatal_os_error(exc):
                    raise FatalError(f"写入文件失败：{exc}") from exc
                attempt += 1
                if self._stop.is_set():
                    raise _Stopped from exc
                self._log(f"连接断开（{exc}），由于该链接不支持 Range，将从头重试…")
                if not self._backoff(attempt, exc, "下载"):
                    raise _Stopped from exc

    # -------------------------------------------------------------- 收尾
    def _finish_or_pause(self, state: JobState) -> Result:
        if self._stop.is_set():
            self._save_state(fsync=True)
            self._set_status("paused", "已暂停，进度已保存，重新运行即可续传")
            return Result("paused", self.output, self._run_bytes, state.size,
                          time.monotonic() - self._t0, self._retries, "已暂停")
        if not state.complete:
            missing = state.size - state.downloaded
            raise FatalError(f"下载未完成，还缺 {human_size(max(0, missing))}")
        return self._finish(state)

    def _finish(self, state: JobState) -> Result:
        assert self._writer is not None
        self._set_status("verifying", "正在校验与收尾…")
        self._save_state(fsync=True)
        writer = self._writer
        writer.fsync()
        writer.close()

        if isinstance(writer, PartsWriter):
            try:
                target = writer.assemble_to(self.merge_path, state.chunks)
            except OSError as exc:
                raise FatalError(f"组装分片失败：{exc}") from exc
        else:
            target = writer.assemble_to(self.merge_path if state.part_mode else self.part_path)
        try:
            actual = target.stat().st_size
        except OSError as exc:
            raise FatalError(f"找不到下载的数据文件：{target}") from exc
        if state.size and actual != state.size:
            raise FatalError(
                f"文件大小校验失败：期望 {human_size(state.size)}，实际 {human_size(actual)}"
            )
        expected = self.sha256 or state.sha256
        if expected:
            self._set_status("verifying", "正在计算 SHA-256…")
            digest = self._sha256_file(target)
            if digest != expected:
                raise FatalError(
                    f"SHA-256 校验失败：期望 {expected}，实际 {digest}。"
                    f"数据保留在 {target}，可重试或检查网络/代理"
                )
            self._log("SHA-256 校验通过")
        else:
            self._log("未提供 SHA-256，已按文件大小校验（建议对重要文件使用 --sha256）")

        self._set_status("finishing", "正在写入目标文件…")
        try:
            os.replace(target, self.output)
        except OSError as exc:
            raise FatalError(
                f"无法写入目标文件 {self.output}：{exc}。请确认文件没有被其它程序占用"
            ) from exc

        # 分片目录/临时分片文件在成功后清理；单文件模式下 part 已被改名，unlink 无害
        writer.cleanup()
        self._cleanup_task_files(remove_state=True, remove_parts=True)
        self._set_status("done", "下载完成")
        self._log(f"完成：{self.output}")
        return Result("done", self.output, self._run_bytes, state.size,
                      time.monotonic() - self._t0, self._retries, None)

    def _cleanup_task_files(self, *, remove_state: bool, remove_parts: bool) -> None:
        if remove_state:
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                pass
        if remove_parts:
            if self.part_mode:
                _remove_tree(self.parts_dir)
            else:
                _remove_file(self.part_path)
            _remove_file(self.merge_path)

    # -------------------------------------------------------------- 状态持久化
    def _start_saver(self) -> None:
        self._saver_stop.clear()
        self._saver = threading.Thread(target=self._saver_loop, name="ghdl-state", daemon=True)
        self._saver.start()

    def _saver_loop(self) -> None:
        while not self._saver_stop.wait(5.0):
            try:
                self._save_state(fsync=True)
            except Exception as exc:  # pragma: no cover
                self._log(f"保存进度失败：{exc}")

    def _save_state(self, *, fsync: bool = False) -> None:
        if self._writer is not None and fsync:
            try:
                self._writer.fsync()
            except (OSError, ValueError):
                pass
        with self._lock:
            state = self._state
            if state is None:
                return
            data = state.to_dict()
        try:
            write_json_atomic(self.state_path, data)
        except OSError as exc:  # pragma: no cover
            self._log(f"写入任务文件失败：{exc}")

    # -------------------------------------------------------------- 其它辅助
    def _shutdown_runtime(self) -> None:
        self._stop.set()
        self._saver_stop.set()
        for resp in self._active_responses():
            try:
                resp.abort()
            except Exception:
                pass
        for thread in self._threads:
            if thread.is_alive():
                thread.join(2.0)
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._lock_file is not None:
            self._lock_file.release()
            self._lock_file = None

    def _backoff(self, attempt: int, err: Exception, what: str) -> bool:
        if self._stop.is_set():
            return False
        if self.max_retries and attempt > self.max_retries:
            raise FatalError(f"{what}失败，已重试 {self.max_retries} 次：{err}")
        delay = min(self.retry_max, self.retry_base * (2 ** (attempt - 1)))
        delay *= random.uniform(0.8, 1.2)
        if isinstance(err, HttpStatusError):
            raw = err.headers.get("retry-after")
            if raw:
                try:
                    delay = max(delay, min(120.0, float(raw)))
                except (TypeError, ValueError):
                    pass
        with self._lock:
            self._retries += 1
            retries = self._retries
        if attempt <= 2 or attempt % 10 == 0:
            self._log(f"{what}中断（{_short(err)}），{delay:.1f}s 后第 {attempt} 次重试…")
        if attempt >= 3:
            self._set_status("waiting", f"网络中断，等待恢复…（已重试 {retries} 次）")
        else:
            self._set_status("waiting", f"连接不稳定，{delay:.0f}s 后重试…")
        return not self._stop.wait(delay)

    def _call_with_retry(self, what: str, fn: Callable[[], object]):
        attempt = 0
        while True:
            if self._stop.is_set():
                raise _Stopped
            try:
                return fn()
            except TlsCertificateError as exc:
                raise FatalError(self._explain_tls(exc)) from exc
            except HttpStatusError as exc:
                if not exc.retryable:
                    raise FatalError(self._explain_http(exc)) from exc
                attempt += 1
                if not self._backoff(attempt, exc, what):
                    raise _Stopped from exc
            except FatalError:
                raise
            except (NetworkError, OSError) as exc:
                attempt += 1
                if not self._backoff(attempt, exc, what):
                    raise _Stopped from exc

    def _explain_tls(self, exc: Exception) -> str:
        return (
            "TLS 证书校验失败，无法建立安全连接。\n"
            "常见原因：公司/校园网代理替换了 HTTPS 证书；系统时间不正确；代理软件未安装根证书。\n"
            "可以：1) 把代理的根证书装进系统“受信任的根证书颁发机构”；"
            "2) 设置正确的代理（--proxy / HTTPS_PROXY）；"
            "3) 临时加 --insecure 跳过校验（不推荐，注意风险）。\n"
            f"详细信息：{exc}"
        )

    def _explain_http(self, exc: HttpStatusError) -> str:
        url = exc.url.split("?")[0]
        if exc.status == 404:
            return (
                f"404 找不到文件：{url}\n"
                "可能原因：链接已失效；仓库是私有的（需要 --token）；Release 资源已被删除。"
            )
        if exc.status in (401, 403):
            remaining = exc.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                return (
                    f"GitHub API 触发限流（403）。请设置 GITHUB_TOKEN 后重试。\n{url}"
                )
            if "api.github.com" in url:
                return f"GitHub API 拒绝访问（{exc.status}）：请检查 Token 权限。\n{url}"
            return (
                f"服务器拒绝访问（HTTP {exc.status}）：{url}\n"
                "如果是私有仓库，请用 --token 提供有 repo 权限的 Token。"
                + (f"\n服务器返回：{exc.snippet}" if exc.snippet else "")
            )
        if exc.status == 429:
            return f"请求过于频繁（429），请稍后重试或提供 Token。\n{url}"
        if 500 <= exc.status < 600:
            return f"GitHub 服务器错误（HTTP {exc.status}），请稍后重试。\n{url}"
        return f"HTTP {exc.status}：{url} {exc.snippet}".strip()

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        size = max(1, path.stat().st_size)
        done = 0
        with open(path, "rb") as fh:
            while True:
                if self._stop.is_set():
                    raise _Stopped
                block = fh.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                done += len(block)
                if size > 64 * 1024 * 1024:
                    self._set_status("verifying", f"正在校验 SHA-256… {done / size * 100:.0f}%")
        return digest.hexdigest()


class _NeedRefresh(Exception):
    """签名地址失效/服务器忽略了 Range，需要重新解析原始链接。"""

    def __init__(self, message: str, gen: int = 0) -> None:
        super().__init__(message)
        self.gen = gen


def _short(err: Exception, limit: int = 160) -> str:
    text = str(err) or type(err).__name__
    text = " ".join(text.split())
    return text[:limit]


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
