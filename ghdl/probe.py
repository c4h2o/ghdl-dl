# SPDX-License-Identifier: MIT
"""探测远端文件：大小、是否支持 Range、真实文件名、GitHub 官方 SHA-256。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import Message
from urllib.parse import quote, unquote, urlsplit

from .http_client import HttpClient, HttpStatusError, NetworkError, Response

_CONTENT_RANGE = re.compile(r"bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)", re.I)

_RELEASE_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/(?P<tag>[^/]+)/(?P<asset>.+)$",
    re.I,
)


@dataclass
class Probe:
    url: str
    final_url: str
    size: int | None
    ranges: bool
    etag: str | None = None
    last_modified: str | None = None
    filename: str | None = None
    content_type: str | None = None


def parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    """返回 (start, end, total)；未知用 None。"""
    if not value:
        return None, None, None
    m = _CONTENT_RANGE.search(value)
    if not m:
        return None, None, None
    start = int(m.group(1)) if m.group(1) is not None else None
    end = int(m.group(2)) if m.group(2) is not None else None
    total = int(m.group(3)) if m.group(3) != "*" else None
    return start, end, total


def parse_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    msg = Message()
    msg["Content-Disposition"] = value
    name = msg.get_filename()
    if name:
        return unquote(name)
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, re.I)
    return unquote(m.group(1)) if m else None


def _looks_like_login_page(resp: Response) -> bool:
    host = (urlsplit(resp.url).hostname or "").lower()
    ctype = (resp.header("content-type") or "").lower()
    return host == "github.com" and ctype.startswith("text/html") and not resp.header("content-disposition")


def probe(client: HttpClient, url: str) -> Probe:
    """用 ``Range: bytes=0-0`` 探测，不会下载整个文件。"""
    try:
        resp = client.open(url, {"Range": "bytes=0-0"})
    except HttpStatusError as exc:
        if exc.status == 416:  # 空文件
            _s, _e, total = parse_content_range(exc.headers.get("content-range"))
            size = total if total is not None else 0
            return Probe(url=url, final_url=exc.url, size=size, ranges=True)
        raise
    try:
        if _looks_like_login_page(resp):
            raise HttpStatusError(
                403,
                resp.url,
                resp.headers,
                b"github.com returned an HTML page instead of the file. "
                b"Private repository? Pass --token, or use the release asset URL.",
                "Login/HTML page",
            )

        etag = resp.header("etag")
        last_modified = resp.header("last-modified")
        ctype = resp.header("content-type")
        filename = parse_content_disposition(resp.header("content-disposition"))
        size: int | None = None
        ranges = False

        if resp.status == 206:
            start, _end, total = parse_content_range(resp.header("content-range"))
            if total is not None and start == 0:
                size = total
                ranges = True
            elif total is not None:
                size = total
                ranges = True
        elif resp.status == 200:
            length = resp.header_int("content-length")
            if length is not None and not resp.header("transfer-encoding"):
                size = length
            ranges = False
        elif resp.status == 416:
            _s, _e, total = parse_content_range(resp.header("content-range"))
            size = total
            ranges = total is not None
        else:  # pragma: no cover - client.open 已把 >=400 变成异常
            raise HttpStatusError(resp.status, resp.url, resp.headers, b"", resp.reason)

        accept_ranges = (resp.header("accept-ranges") or "").lower()
        if accept_ranges == "none":
            ranges = False
        if resp.header("content-encoding") and resp.header("content-encoding").lower() not in ("identity", ""):
            # 压缩过的响应无法按字节定位
            ranges = False

        return Probe(
            url=url,
            final_url=resp.url,
            size=size,
            ranges=ranges,
            etag=etag,
            last_modified=last_modified,
            filename=filename,
            content_type=ctype,
        )
    finally:
        resp.close()


def github_release_digest(client: HttpClient, url: str) -> tuple[str | None, int | None, str | None]:
    """对 GitHub Release 资源，尝试从官方 API 取 SHA-256 与大小。

    返回 ``(sha256_hex or None, size or None, asset_name or None)``；任何失败都静默返回空。
    """
    m = _RELEASE_RE.match(url.split("?", 1)[0])
    if not m:
        return None, None, None
    owner, repo, tag, asset = m.group("owner"), m.group("repo"), m.group("tag"), m.group("asset")
    asset_name = unquote(asset)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if tag in ("latest", "latest/download"):
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    else:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"
    try:
        resp = client.open(api_url, headers)
        try:
            payload = json.loads(resp.read_all(4 * 1024 * 1024).decode("utf-8"))
        finally:
            resp.close()
    except (NetworkError, HttpStatusError, ValueError, OSError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    for item in payload.get("assets") or []:
        if str(item.get("name")) != asset_name:
            continue
        digest = item.get("digest")
        sha = None
        if isinstance(digest, str) and digest.lower().startswith("sha256:"):
            candidate = digest.split(":", 1)[1].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", candidate):
                sha = candidate
        try:
            size = int(item.get("size")) if item.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        return sha, size, asset_name
    return None, None, asset_name
