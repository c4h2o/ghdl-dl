# SPDX-License-Identifier: MIT
"""测试用的本地 HTTP 服务器：模拟 Range、断流、签名过期、限流等场景。"""

from __future__ import annotations

import socket
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


class Store:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.hits: dict[str, int] = defaultdict(int)
        self.ranges: dict[str, int] = defaultdict(int)
        self.drops: dict[str, int] = defaultdict(int)
        self.expired: dict[str, int] = defaultdict(int)
        self.gate: dict[str, int] = {}
        self.delay: dict[str, float] = {}
        self.drop_after: dict[str, int] = {}
        self.token_ttl = 0.3
        self._token_seq = 0
        self._tokens: dict[str, float] = {}
        self._cdn_lock = threading.Lock()

    def put(self, name: str, data: bytes) -> None:
        self.files[name] = data

    def new_token(self) -> str:
        with self._cdn_lock:
            self._token_seq += 1
            token = f"t{self._token_seq}"
            self._tokens[token] = time.monotonic() + self.token_ttl
            return token

    def use_token(self, token: str) -> bool:
        with self._cdn_lock:
            expiry = self._tokens.get(token)
            return expiry is not None and time.monotonic() <= expiry


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ghdl-test"

    def log_message(self, *args) -> None:  # noqa: A003 - 静默
        pass

    # -- 基础工具 ---------------------------------------------------------
    def _send_simple(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except OSError:
                pass
        self.close_connection = True

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
        if not parts:
            self._send_simple(404, b"not found")
            return
        route = parts[0]
        if route == "file" and len(parts) == 2:
            self._serve(parts[1], mode="normal")
        elif route == "flaky" and len(parts) == 2:
            self._serve(parts[1], mode="flaky")
        elif route == "norange" and len(parts) == 2:
            self._serve(parts[1], mode="norange")
        elif route == "chunked" and len(parts) == 2:
            self._serve(parts[1], mode="chunked")
        elif route == "gate" and len(parts) == 2:
            self._gate(parts[1])
        elif route == "expire" and len(parts) == 2:
            token = STORE.new_token()
            self._redirect(f"/cdn/{token}/{parts[1]}")
        elif route == "cdn" and len(parts) == 3:
            if not STORE.use_token(parts[1]):
                STORE.expired[parts[2]] += 1
                self._send_simple(403, b"<Error><Code>AuthenticationFailed</Code></Error>")
                return
            self._serve(parts[2], mode="normal")
        else:
            self._send_simple(404, b"not found")

    def _gate(self, name: str) -> None:
        with STORE.lock:
            left = STORE.gate.get(name, 0)
            if left > 0:
                STORE.gate[name] = left - 1
        if left > 0:
            STORE.hits[name] += 1
            self._send_simple(503, b"temporarily unavailable", {"Retry-After": "1"})
            return
        self._serve(name, mode="normal")

    # -- 核心：按 Range 返回 ----------------------------------------------
    def _serve(self, name: str, mode: str = "normal") -> None:
        data = STORE.files.get(name)
        if data is None:
            self._send_simple(404, b"no such file")
            return
        STORE.hits[name] += 1
        total = len(data)

        if mode == "chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for offset in range(0, total, 65536):
                    piece = data[offset:offset + 65536]
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
            self.close_connection = True
            return

        start, end = 0, total - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and mode != "norange":
            try:
                spec = range_header.split("=", 1)[1]
                first, _, last = spec.partition("-")
                start = int(first) if first else 0
                end = int(last) if last else total - 1
                if start > end or start >= total:
                    self._send_simple(416, b"", {"Content-Range": f"bytes */{total}"})
                    return
                end = min(end, total - 1)
                status = 206
                STORE.ranges[name] += 1
            except (ValueError, IndexError):
                self._send_simple(400, b"bad range")
                return

        body = data[start:end + 1]
        self.send_response(status)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "none" if mode == "norange" else "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Connection", "close")
        self.end_headers()

        delay = STORE.delay.get(name, 0.0)
        piece_size = 32768 if mode == "flaky" else 65536
        limit = STORE.drop_after.get(name, 0) if mode == "flaky" else 0
        sent = 0
        try:
            while sent < len(body):
                piece = body[sent:sent + piece_size]
                self.wfile.write(piece)
                self.wfile.flush()
                sent += len(piece)
                if delay:
                    time.sleep(delay)
                if limit and sent >= limit:
                    STORE.drops[name] += 1
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return
        except OSError:
            self.close_connection = True
        self.close_connection = True


class TestServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "TestServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)
