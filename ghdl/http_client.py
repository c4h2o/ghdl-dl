# SPDX-License-Identifier: MIT
"""极简 HTTP 客户端。

只做下载器需要的几件事：

* 手动跟随 3xx 重定向（这样可以拿到 GitHub 签名 CDN 的真实地址，过期后可重新解析）；
* 发送 ``Range`` 头做分段下载；
* 支持 HTTP/HTTPS 代理（含 CONNECT 隧道与代理认证）；
* 只向 github.com 系列域名发送 Token，避免把凭证泄漏给签名 CDN。

所有连接都使用 ``Connection: close``：每个分段请求一条连接，生命周期简单，
不会出现 keep-alive 连接被服务端悄悄掐断却复用到下一个请求的问题。
"""

from __future__ import annotations

import base64
import http.client
import os
import socket
import ssl
from urllib.parse import unquote, urljoin, urlsplit

DEFAULT_UA = "ghdl/1.0 (+https://github.com) Python"

#: 允许携带 GitHub Token 的主机（后缀匹配）。
_AUTH_HOSTS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
)

_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class NetworkError(Exception):
    """连接/读取层的暂时性错误，调用方应当重试。"""


class TlsCertificateError(NetworkError):
    """TLS 证书校验失败（重试也不会好，需要用户处理证书/代理）。"""


class HttpStatusError(Exception):
    """服务端返回了非 2xx 状态码。"""

    def __init__(
        self,
        status: int,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        reason: str = "",
    ) -> None:
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.body = body
        self.reason = reason
        super().__init__(f"HTTP {status} {reason} ({url})")

    @property
    def retryable(self) -> bool:
        return self.status in (408, 425, 429) or 500 <= self.status < 600

    @property
    def snippet(self) -> str:
        text = self.body[:800].decode("utf-8", "replace").strip()
        text = " ".join(text.split())
        return text[:300]


class Response:
    """对 ``http.client.HTTPResponse`` 的薄封装。"""

    __slots__ = ("status", "reason", "headers", "url", "_conn", "_resp", "_closed")

    def __init__(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse, url: str) -> None:
        self._conn = conn
        self._resp = resp
        self._closed = False
        self.url = url
        self.status = resp.status
        self.reason = resp.reason or ""
        self.headers = {k.lower(): v for k, v in resp.getheaders()}

    # -- 头部工具 ---------------------------------------------------------
    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def header_int(self, name: str) -> int | None:
        raw = self.headers.get(name.lower())
        if raw is None:
            return None
        try:
            return int(raw.strip())
        except (TypeError, ValueError):
            return None

    # -- 读取 -------------------------------------------------------------
    def read(self, n: int = -1) -> bytes:
        try:
            return self._resp.read(n)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:  # 含超时、连接重置
            raise NetworkError(f"读取数据失败: {exc or type(exc).__name__}") from exc

    def read_all(self, limit: int = 64 * 1024) -> bytes:
        """读取少量内容（错误页/JSON），最多 limit 字节。"""
        chunks: list[bytes] = []
        got = 0
        try:
            while got < limit:
                piece = self._resp.read(min(8192, limit - got))
                if not piece:
                    break
                chunks.append(piece)
                got += len(piece)
        except (OSError, http.client.HTTPException):
            pass
        return b"".join(chunks)

    # -- 关闭 -------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._resp.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    def abort(self) -> None:
        """从其它线程强行打断阻塞中的 read()（用于“暂停/取消”）。"""
        sock = getattr(self._conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _basic_auth_header(userinfo: str) -> str:
    raw = base64.b64encode(unquote(userinfo).encode("utf-8")).decode("ascii")
    return "Basic " + raw


class HttpClient:
    """处理重定向、认证、代理的 GET 客户端。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        proxy: str | None = None,
        user_agent: str = DEFAULT_UA,
        insecure: bool = False,
    ) -> None:
        self.token = (token or "").strip() or None
        self.timeout = timeout
        self.user_agent = user_agent
        self.insecure = insecure
        self.proxy = self._parse_proxy(proxy)
        self._ctx = self._make_context(insecure)

    # -- 初始化辅助 -------------------------------------------------------
    @staticmethod
    def _make_context(insecure: bool) -> ssl.SSLContext:
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        try:
            return ssl.create_default_context()
        except Exception:  # pragma: no cover - 极少见
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_default_certs()
            return ctx

    @staticmethod
    def _parse_proxy(proxy: str | None) -> tuple[str, int, dict[str, str]] | None:
        raw = proxy
        if not raw:
            raw = (
                os.environ.get("HTTPS_PROXY")
                or os.environ.get("https_proxy")
                or os.environ.get("ALL_PROXY")
                or os.environ.get("all_proxy")
            )
        if not raw:
            return None
        if "://" not in raw:
            raw = "http://" + raw
        parts = urlsplit(raw)
        if not parts.hostname:
            return None
        scheme = (parts.scheme or "http").lower()
        port = parts.port or (443 if scheme == "https" else 80)
        headers: dict[str, str] = {}
        if parts.username:
            userinfo = parts.username + (":" + parts.password if parts.password else "")
            headers["Proxy-Authorization"] = _basic_auth_header(userinfo)
        return parts.hostname, port, headers

    def _should_bypass_proxy(self, host: str) -> bool:
        host = host.lower()
        if host in _LOCAL_NAMES or host.startswith("127.") or host == "::1":
            return True
        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        for item in no_proxy.split(","):
            item = item.strip().lower()
            if item and (host == item or host.endswith("." + item.lstrip("*."))):
                return True
        return False

    # -- 请求头 -----------------------------------------------------------
    def _headers_for(self, url: str, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",  # 关键：压缩会破坏按字节分段
            "Connection": "close",
        }
        host = (urlsplit(url).hostname or "").lower()
        if self.token and any(host == h or host.endswith("." + h) for h in _AUTH_HOSTS):
            headers["Authorization"] = "Bearer " + self.token
        if extra:
            headers.update(extra)
        return headers

    # -- 建立连接 ---------------------------------------------------------
    def _connect(self, url: str) -> tuple[http.client.HTTPConnection, str]:
        parts = urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise NetworkError(f"不支持的协议: {scheme}")
        host = parts.hostname or ""
        if not host:
            raise NetworkError(f"无效的 URL: {url}")
        port = parts.port or (443 if scheme == "https" else 80)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query

        if self.proxy and not self._should_bypass_proxy(host):
            p_host, p_port, p_headers = self.proxy
            if scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    p_host, p_port, timeout=self.timeout, context=self._ctx
                )
                conn.set_tunnel(host, port, headers=p_headers or None)
                return conn, target
            conn = http.client.HTTPConnection(p_host, p_port, timeout=self.timeout)
            # 明文 HTTP 走代理时使用 absolute-form 请求行
            return conn, f"http://{host}:{port}{target}"

        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        return conn, target

    # -- 主入口 -----------------------------------------------------------
    def open(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        max_redirects: int = 10,
    ) -> Response:
        """GET 请求并跟随重定向，返回已打开的响应（4xx/5xx 也会返回，由调用方判断）。"""
        current = url
        for _ in range(max_redirects + 1):
            conn, target = self._connect(current)
            try:
                conn.request("GET", target, headers=self._headers_for(current, headers))
                raw = conn.getresponse()
            except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout) as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                text = str(exc or type(exc).__name__)
                if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text.upper():
                    raise TlsCertificateError(
                        f"TLS 证书校验失败（{urlsplit(current).hostname}）：{text}"
                    ) from exc
                raise NetworkError(f"连接 {urlsplit(current).hostname} 失败: {text}") from exc

            if raw.status in _REDIRECT_CODES:
                location = raw.getheader("Location")
                try:
                    raw.read(65536)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                if not location:
                    raise NetworkError(f"服务器返回 {raw.status} 但没有 Location 头")
                current = urljoin(current, location.strip())
                continue

            resp = Response(conn, raw, current)
            if raw.status >= 400:
                body = resp.read_all()
                try:
                    resp.close()
                except Exception:
                    pass
                raise HttpStatusError(raw.status, current, resp.headers, body, raw.reason or "")
            return resp

        raise NetworkError(f"重定向次数过多: {url}")
