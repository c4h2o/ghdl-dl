# SPDX-License-Identifier: MIT
"""回归测试：abort() 后 read() 抛 ValueError/EOFError 应转成 NetworkError。

macOS 上 request_stop() 通过 sock.shutdown()+close() 打断阻塞中的 read()，
HTTPResponse.read() 可能抛出 ValueError("I/O operation on closed file")，
旧代码未捕获，导致暂停被误判为 error。
"""
from __future__ import annotations

import socket
import threading
import unittest

from ghdl.http_client import HttpClient, NetworkError, Response


class _FakeResp:
    """模拟 http.client.HTTPResponse：read() 抛 ValueError（连接已关闭）。"""

    status = 200
    reason = "OK"

    def getheaders(self):
        return []

    def read(self, n: int = -1) -> bytes:
        raise ValueError("I/O operation on closed file")


class _FakeConn:
    sock = None

    def close(self) -> None:
        pass


class ResponseReadTest(unittest.TestCase):
    def test_read_wraps_valueerror(self) -> None:
        resp = Response(_FakeConn(), _FakeResp(), "http://example/x")
        with self.assertRaises(NetworkError):
            resp.read(10)

    def test_abort_then_read_is_network_error(self) -> None:
        """abort() 之后继续 read() 也应得到 NetworkError，而不是裸 ValueError。"""
        client = HttpClient()
        conn = _FakeConn()
        raw = _FakeResp()
        resp = Response(conn, raw, "http://example/x")
        resp.abort()
        with self.assertRaises(NetworkError):
            resp.read(10)


if __name__ == "__main__":
    unittest.main()
