# SPDX-License-Identifier: MIT
"""命令行界面。"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .engine import DownloadJob, Result, Snapshot
from .http_client import HttpClient
from .model import human_eta, human_size
from .probe import github_release_digest, probe as probe_url

_STATUS_LABEL = {
    "idle": "准备中",
    "starting": "准备中",
    "probing": "连接服务器",
    "downloading": "下载中",
    "waiting": "等待重连",
    "verifying": "校验中",
    "finishing": "收尾中",
    "done": "已完成",
    "paused": "已暂停",
    "error": "出错",
    "skipped": "已存在",
}

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


def _enable_ansi() -> None:
    if os.name != "nt" or not sys.stdout.isatty():
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def parse_size(text: str) -> int:
    text = text.strip().replace("_", "")
    if not text:
        raise argparse.ArgumentTypeError("大小不能为空")
    units = {
        "k": 1024, "kb": 1024, "kib": 1024,
        "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
        "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
    }
    lower = text.lower()
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if lower.endswith(suffix):
            try:
                return int(float(lower[: -len(suffix)].strip()) * mult)
            except ValueError:
                break
    try:
        return int(float(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无法识别的大小：{text}") from exc


class Display:
    """终端进度显示：TTY 下原地刷新，重定向时每 5 秒打印一行。"""

    def __init__(self, stream=None, interval: float = 5.0) -> None:
        self.stream = stream or sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.interval = interval
        self._drawn = 0
        self._last = 0.0
        try:
            self.columns = shutil.get_terminal_size((100, 24)).columns
        except Exception:
            self.columns = 100

    def _draw(self, lines: list[str]) -> None:
        if not self.tty:
            line = lines[0]
            if len(lines) > 1 and lines[1]:
                line += "  " + lines[1]
            self.stream.write(line + "\n")
            self.stream.flush()
            return
        buf: list[str] = []
        if self._drawn:
            buf.append(f"\x1b[{self._drawn}A")
        for line in lines:
            buf.append("\x1b[2K" + line[: max(20, self.columns - 1)] + "\n")
        self.stream.write("".join(buf))
        self.stream.flush()
        self._drawn = len(lines)

    @staticmethod
    def _bar(frac: float, width: int = 30) -> str:
        frac = max(0.0, min(1.0, frac))
        filled = int(round(width * frac))
        return "█" * filled + "░" * (width - filled)

    def update(self, s: Snapshot, force: bool = False) -> None:
        now = time.monotonic()
        if not self.tty and not force and now - self._last < self.interval:
            return
        self._last = now
        if s.total > 0:
            line1 = (
                f"[{self._bar(s.fraction)}] {s.fraction * 100:5.1f}%  "
                f"{human_size(s.downloaded)} / {human_size(s.total)}  "
                f"{human_size(s.speed)}/s  ETA {human_eta(s.eta)}"
            )
        else:
            line1 = f"[{self._bar(0)}]  {human_size(s.downloaded)}  {human_size(s.speed)}/s"
        if s.status in ("waiting", "paused", "verifying", "error") and s.message:
            detail = s.message
        elif s.chunk_total:
            detail = (
                f"{_STATUS_LABEL.get(s.status, s.status)} · 连接 {s.active}/{s.connections}"
                f" · 分段 {s.chunk_done}/{s.chunk_total} · 重试 {s.retries}"
            )
        else:
            detail = _STATUS_LABEL.get(s.status, s.status)
        lines = [line1, detail]
        if self.tty and s.status == "downloading" and s.workers:
            parts = []
            for idx, done, length in s.workers[:12]:
                pct = int(done / length * 100) if length else 100
                parts.append(f"#{idx + 1} {pct}%")
            lines.append("  ".join(parts))
        self._draw(lines)

    def close(self) -> None:
        if self.tty and self._drawn:
            self.stream.write("\n")
            self.stream.flush()
            self._drawn = 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghdl-dl",
        description="GitHub 大文件多连接分段下载器（断点续传 / 断线自动重连 / SHA-256 校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  ghdl https://github.com/owner/repo/releases/download/v1.0/big.zip
  ghdl-dl <URL> -o D:\\Downloads -n 12
  ghdl-dl <URL> -o D:\\Downloads\\big.zip --sha256 <64位哈希>
  ghdl-dl <URL> --parts            # 每段一个文件，下完自动拼接
  ghdl-dl <URL> --token ghp_xxx    # 私有仓库
  ghdl-dl <URL> -x http://127.0.0.1:7890   # 走代理（也读 HTTPS_PROXY 环境变量）
  ghdl-dl <URL> --info             # 只看文件大小/是否支持分段
""",
    )
    parser.add_argument("urls", nargs="+", metavar="URL", help="文件链接（可多个）")
    parser.add_argument("-o", "--output", help="保存为文件或目录；默认当前目录")
    parser.add_argument("-n", "--connections", type=int, default=8, help="并发连接数（默认 8）")
    parser.add_argument("--chunk-size", type=parse_size, help="分段大小，如 8M、32MiB")
    parser.add_argument("--sha256", help="下载完成后校验 SHA-256")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
        help="GitHub Token（默认读环境变量 GITHUB_TOKEN/GH_TOKEN）",
    )
    parser.add_argument("-x", "--proxy", help="HTTP(S) 代理，如 http://127.0.0.1:7890")
    parser.add_argument("--timeout", type=float, default=30.0, help="连接/读取超时秒数（默认 30）")
    parser.add_argument("--retries", type=int, default=0, help="每段最大重试次数，0=一直重试（默认）")
    parser.add_argument("--retry-base", type=float, default=1.0, help="首次重试等待秒数（默认 1）")
    parser.add_argument("--retry-max", type=float, default=30.0, help="重试等待上限秒数（默认 30）")
    parser.add_argument("--parts", action="store_true", help="每段写一个文件，全部完成后拼接")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有进度，重新开始")
    parser.add_argument("--restart", action="store_true", help="删除已有进度并重新下载")
    parser.add_argument("--force", action="store_true", help="目标文件已存在时也重新下载")
    parser.add_argument("--no-api", action="store_true", help="不查询 GitHub API 获取官方 SHA-256")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（不推荐）")
    parser.add_argument("--info", action="store_true", help="只探测文件信息，不下载")
    parser.add_argument("-q", "--quiet", action="store_true", help="不显示进度，只输出结果")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出更详细日志")
    parser.add_argument("--version", action="version", version=f"ghdl-dl {__version__}")
    return parser


def _run_info(args) -> int:
    client = HttpClient(token=args.token, timeout=args.timeout, proxy=args.proxy, insecure=args.insecure)
    for url in args.urls:
        try:
            info = probe_url(client, url)
        except Exception as exc:
            print(f"探测失败：{exc}", file=sys.stderr)
            if "CERTIFICATE" in str(exc).upper() or "证书" in str(exc):
                print(
                    "提示：这是 TLS 证书校验失败。可安装代理的根证书，"
                    "或临时加 --insecure 跳过校验（不推荐）。",
                    file=sys.stderr,
                )
            return 1
        print(f"链接       {url}")
        print(f"真实地址   {info.final_url.split('?')[0]}")
        print(f"大小       {human_size(info.size)}" + (f"（{info.size} 字节）" if info.size else ""))
        print(f"Range      {'支持，可多线程分段/续传' if info.ranges else '不支持（动态生成，只能单流）'}")
        if info.filename:
            print(f"文件名     {info.filename}")
        if info.etag:
            print(f"ETag       {info.etag}")
        if not args.no_api:
            sha, size, _name = github_release_digest(client, url)
            if sha:
                print(f"官方SHA256 {sha}")
            if size is not None:
                print(f"API 大小   {human_size(size)}")
        print()
    return 0


_CURRENT: dict[str, object] = {"job": None, "count": 0}


def _install_sigint() -> None:
    def handler(signum, frame):  # noqa: ARG001
        _CURRENT["count"] = int(_CURRENT.get("count") or 0) + 1
        job = _CURRENT.get("job")
        if _CURRENT["count"] >= 2 or job is None:
            print("\n强制退出。", file=sys.stderr)
            os._exit(130)
        print("\n正在暂停并保存进度…（再按一次 Ctrl+C 强制退出）", file=sys.stderr)
        job.request_stop()  # type: ignore[union-attr]

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        pass


def _print_result(result: Result) -> None:
    if result.status == "done":
        speed = result.downloaded / result.seconds if result.seconds > 0 else 0
        print(_c(
            f"✔ 完成：{result.output}\n  用时 {result.seconds:.0f}s，平均 {human_size(speed)}/s",
            "32",
        ))
    elif result.status == "skipped":
        print(_c(f"✔ 文件已存在，跳过：{result.output}", "32"))
    elif result.status == "paused":
        print(_c("⏸ 已暂停，进度已保存。", "33"))
    else:
        print(_c(f"✘ 失败：{result.error}", "31"))


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    _enable_ansi()
    args = _build_parser().parse_args(argv)
    if args.info:
        return _run_info(args)

    if len(args.urls) > 1:
        if not args.output or not (args.output.endswith(("/", "\\")) or Path(args.output).is_dir()):
            print("下载多个链接时，-o 必须是已存在的目录（或以斜杠结尾）", file=sys.stderr)
            return 2

    target = args.output if args.output else os.getcwd() + os.sep
    _install_sigint()
    display = Display()
    results: list[Result] = []

    for index, url in enumerate(args.urls):
        if index and not args.quiet:
            print()
        job = DownloadJob(
            url,
            target,
            connections=min(32, max(1, args.connections)),
            chunk_size=args.chunk_size,
            token=args.token,
            sha256=args.sha256,
            timeout=args.timeout,
            max_retries=args.retries,
            retry_base=args.retry_base,
            retry_max=args.retry_max,
            proxy=args.proxy,
            resume=not args.no_resume,
            restart=args.restart,
            part_mode=args.parts,
            api_digest=not args.no_api,
            insecure=args.insecure,
            force=args.force,
            log=(lambda m: _log(display, m)) if not args.quiet else None,
        )
        _CURRENT["job"] = job
        _CURRENT["count"] = 0
        if not args.quiet:
            print(f"\n{_c('下载', '36')} {url}")
        box: dict[str, Result] = {}

        def worker(job: DownloadJob = job, box: dict = box) -> None:
            box["result"] = job.run()

        thread = threading.Thread(target=worker, name="ghdl-run", daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                thread.join(0.2)
                if not args.quiet:
                    display.update(job.snapshot())
        finally:
            _CURRENT["job"] = None
            display.close()
        result = box.get("result") or Result("error", error="内部错误（下载线程未返回结果）")
        results.append(result)
        if not args.quiet:
            _print_result(result)
        if result.status in ("paused", "error"):
            break

    if args.quiet and results:
        last = results[-1]
        print(f"{last.output} {last.status} {last.error or ''}".strip())

    if any(r.status == "error" for r in results):
        return 1
    if any(r.status == "paused" for r in results):
        print("\n进度已保存。重新运行同样的命令即可继续下载。")
        return 3
    return 0


def _log(display: Display, message: str) -> None:
    display.close()
    for line in str(message).splitlines():
        print("  · " + line)
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
