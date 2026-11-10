# SPDX-License-Identifier: MIT
"""数据落盘：单文件随机写（默认）、每段一个文件（可选）、单流顺序写。"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import BinaryIO

from .model import Chunk

_BLOCK = 1024 * 1024


def _write_all(fd: int, data: bytes, where: str = "") -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - 磁盘满通常会直接抛异常
            raise OSError(f"写入失败{where}")
        view = view[written:]


class Writer:
    """落盘器接口。"""

    size: int = 0

    def write(self, chunk: Chunk, offset_in_chunk: int, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def reset(self) -> None:  # pragma: no cover - 仅单流模式使用
        raise NotImplementedError

    def fsync(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def assemble_to(self, dest: Path) -> Path:  # pragma: no cover
        raise NotImplementedError

    def cleanup(self) -> None:  # pragma: no cover
        raise NotImplementedError


class SingleFileWriter(Writer):
    """预分配一个大文件，各线程用 pwrite / lseek+write 写到各自偏移。

    好处：不需要最后再把多个 GB 拼接一遍（少一次全量读写、不少一份磁盘空间），
    文件本身就是“已组装”状态，随时可以按偏移续传。
    """

    def __init__(self, path: Path, size: int) -> None:
        self.path = Path(path)
        self.size = int(size)
        self._lock = threading.Lock()
        self._pwrite = hasattr(os, "pwrite")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        self._fd = os.open(self.path, flags, 0o666)
        try:
            os.ftruncate(self._fd, self.size)
        except OSError:
            os.close(self._fd)
            raise

    def write(self, chunk: Chunk, offset_in_chunk: int, data: bytes) -> None:
        absolute = chunk.start + offset_in_chunk
        with self._lock:
            if self._fd < 0:
                raise OSError("文件已关闭")
            if self._pwrite:
                view = memoryview(data)
                pos = absolute
                while view:
                    n = os.pwrite(self._fd, view, pos)  # type: ignore[attr-defined]
                    if n <= 0:  # pragma: no cover
                        raise OSError(f"写入失败 @{pos}")
                    view = view[n:]
                    pos += n
            else:
                os.lseek(self._fd, absolute, os.SEEK_SET)
                _write_all(self._fd, data, f" @{absolute}")

    def reset(self) -> None:
        with self._lock:
            os.ftruncate(self._fd, 0)

    def fsync(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                fd, self._fd = self._fd, -1
                try:
                    os.fsync(fd)
                except OSError:
                    pass
                os.close(fd)

    def assemble_to(self, dest: Path) -> Path:
        """已经是单文件，无需拼接。"""
        return self.path

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except (FileNotFoundError, OSError):
            pass


class PartsWriter(Writer):
    """每个分段写一个独立文件，全部完成后按顺序拼接成最终文件。"""

    def __init__(self, directory: Path, size: int) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.size = int(size)
        self._lock = threading.Lock()
        self._files: dict[int, BinaryIO] = {}

    def part_path(self, index: int) -> Path:
        return self.directory / f"part_{index:05d}.bin"

    def _file(self, index: int) -> BinaryIO:
        fh = self._files.get(index)
        if fh is None:
            path = self.part_path(index)
            fh = open(path, "r+b" if path.exists() else "w+b")
            self._files[index] = fh
        return fh

    def write(self, chunk: Chunk, offset_in_chunk: int, data: bytes) -> None:
        with self._lock:
            fh = self._file(chunk.index)
            fh.seek(offset_in_chunk)
            fh.write(data)

    def reset(self) -> None:
        with self._lock:
            for fh in self._files.values():
                fh.seek(0)
                fh.truncate(0)

    def fsync(self) -> None:
        with self._lock:
            for fh in self._files.values():
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        with self._lock:
            for fh in self._files.values():
                try:
                    fh.flush()
                    fh.close()
                except (OSError, ValueError):
                    pass
            self._files.clear()

    def assemble_to(self, dest: Path, chunks: list[Chunk] | None = None) -> Path:
        """按段号顺序拼接；dest 由调用方负责原子改名。"""
        dest = Path(dest)
        if chunks is not None:
            count = len(chunks)
        else:
            count = len(list(self.directory.glob("part_*.bin")))
        if not count:
            raise OSError("没有可拼接的分片文件")
        tmp = Path(str(dest) + ".joining")
        with open(tmp, "wb") as out:
            for index in range(count):
                path = self.part_path(index)
                if not path.exists():
                    raise OSError(f"分片文件缺失：{path.name}")
                with open(path, "rb") as src:
                    shutil.copyfileobj(src, out, _BLOCK)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
        return dest

    def cleanup(self) -> None:
        self.close()
        shutil.rmtree(self.directory, ignore_errors=True)


class StreamWriter(Writer):
    """不支持 Range 的链接使用：从头顺序写，失败就重来。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.size = 0
        self._lock = threading.Lock()
        self._fh = open(self.path, "wb")

    def write(self, data: bytes) -> None:  # type: ignore[override]
        with self._lock:
            self._fh.write(data)

    def reset(self) -> None:
        with self._lock:
            self._fh.seek(0)
            self._fh.truncate(0)

    def fsync(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except (OSError, ValueError):
                pass

    def assemble_to(self, dest: Path) -> Path:
        return self.path

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except (FileNotFoundError, OSError):
            pass
