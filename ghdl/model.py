# SPDX-License-Identifier: MIT
"""任务模型：分段、进度状态持久化、文件锁、文件名处理。"""

from __future__ import annotations

import errno
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_VERSION = 1

# Windows 不允许的字符 + 控制字符
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def human_size(num: float | int | None) -> str:
    if num is None:
        return "未知"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds or seconds > 86400 * 7:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def sanitize_filename(name: str, default: str = "download.bin") -> str:
    """把服务端给的文件名变成安全的 Windows 文件名。"""
    name = (name or "").strip().strip('"')
    name = name.replace("\\", "/").split("/")[-1]
    name = _BAD_FILENAME.sub("_", name)
    name = name.strip(" .")
    if not name or name in (".", ".."):
        return default
    stem, dot, ext = name.rpartition(".")
    if dot and stem.upper() in _RESERVED_NAMES:
        name = f"_{name}"
    elif name.upper() in _RESERVED_NAMES:
        name = f"_{name}"
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 20:
            name = stem[: 180 - len(ext) - 1] + "." + ext
        else:
            name = name[:180]
    return name


def filename_from_url(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    base = path.rsplit("/", 1)[-1]
    from urllib.parse import unquote

    return sanitize_filename(unquote(base))


@dataclass
class Chunk:
    """一段字节区间，``end`` 为开区间。"""

    index: int
    start: int
    end: int
    done: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def finished(self) -> bool:
        return self.done >= self.length

    def to_json(self) -> list[int]:
        return [self.start, self.end, self.done]

    @classmethod
    def from_json(cls, index: int, raw) -> "Chunk":
        start, end, done = (int(raw[0]), int(raw[1]), int(raw[2]) if len(raw) > 2 else 0)
        done = max(0, min(done, end - start))
        return cls(index, start, end, done)


def plan_chunks(
    total: int,
    connections: int,
    chunk_size: int | None = None,
    *,
    min_chunk: int = 2 * 1024 * 1024,
    max_chunk: int = 64 * 1024 * 1024,
    max_chunks: int = 4096,
) -> list[Chunk]:
    """把文件切成若干段。

    默认让每个连接至少分到约 4 段，这样先完成的连接可以继续领新段，
    速度慢的段不会拖住整体；段大小限定在 [min_chunk, max_chunk]。
    """
    total = int(total)
    if total <= 0:
        return [Chunk(0, 0, 0)]
    connections = max(1, int(connections))
    if chunk_size and chunk_size > 0:
        size = int(chunk_size)
    else:
        size = total // (connections * 4)
        size = max(min_chunk, min(max_chunk, size))
    # 段数太多会让状态文件过大，收敛一下
    if total // size > max_chunks:
        size = max(size, -(-total // max_chunks))
    size = max(1, min(size, total))

    chunks: list[Chunk] = []
    pos = 0
    while pos < total:
        end = min(total, pos + size)
        chunks.append(Chunk(len(chunks), pos, end))
        pos = end
    return chunks


@dataclass
class JobState:
    """可持久化的下载状态。"""

    url: str
    output: str
    size: int
    chunk_size: int
    ranges: bool
    chunks: list[Chunk]
    etag: str | None = None
    last_modified: str | None = None
    sha256: str | None = None
    part_mode: bool = False
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    @property
    def downloaded(self) -> int:
        return sum(c.done for c in self.chunks)

    @property
    def complete(self) -> bool:
        return bool(self.chunks) and all(c.finished for c in self.chunks)

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "url": self.url,
            "output": self.output,
            "size": self.size,
            "chunk_size": self.chunk_size,
            "ranges": self.ranges,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "sha256": self.sha256,
            "part_mode": self.part_mode,
            "created": self.created,
            "updated": self.updated,
            "chunks": [c.to_json() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobState":
        if int(data.get("version", 0)) != STATE_VERSION:
            raise ValueError(f"不支持的任务文件版本: {data.get('version')}")
        chunks = [Chunk.from_json(i, raw) for i, raw in enumerate(data.get("chunks") or [])]
        size = int(data.get("size") or 0)
        if size < 0:
            raise ValueError("任务文件中的文件大小无效")
        if chunks and chunks[-1].end != size:
            raise ValueError("任务文件中的分段与文件大小不一致")
        return cls(
            url=str(data.get("url") or ""),
            output=str(data.get("output") or ""),
            size=size,
            chunk_size=int(data.get("chunk_size") or 0),
            ranges=bool(data.get("ranges")),
            chunks=chunks,
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            sha256=data.get("sha256"),
            part_mode=bool(data.get("part_mode")),
            created=float(data.get("created") or time.time()),
            updated=float(data.get("updated") or time.time()),
        )


def write_json_atomic(path: Path, data: dict) -> None:
    """先写临时文件、fsync、再原子替换，保证崩溃时不会留下半个 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows 上杀毒/索引偶尔占用文件
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def save_state_atomic(path: Path, state: JobState) -> None:
    state.updated = time.time()
    write_json_atomic(path, state.to_dict())


def load_state(path: Path) -> JobState | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    try:
        return JobState.from_dict(raw)
    except (TypeError, ValueError, KeyError):
        return None


class LockBusyError(RuntimeError):
    pass


class FileLock:
    """跨平台文件锁：进程崩溃时由操作系统自动释放，不会留下“僵尸锁”。

    Windows 用 ``msvcrt.locking``，其它平台用 ``fcntl.flock``，都锁在文件第 0 字节。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK, 13, 11, 33, 36):
                raise LockBusyError(f"文件正被另一个下载进程使用: {self.path}") from exc
            raise
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
