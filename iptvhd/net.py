"""极简 HTTP 客户端：只依赖标准库，但带上首字节延迟与吞吐计量。

探测直播流需要三样东西：HTTP 状态、内容类型、以及「下载 N 字节花了多久」。
urllib 本身不提供后两者的计时能力，所以这里自己用 http.client 实现。
"""

from __future__ import annotations

import gzip
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from . import __version__

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_MAX_REDIRECTS = 5


@dataclass(slots=True)
class Response:
    url: str = ""
    status: int = 0
    headers: dict = None
    body: bytes = b""
    ttfb_ms: int = 0        # 首字节延迟
    elapsed_ms: int = 0     # 首字节到读完的耗时
    error: str = ""
    # 读体被截止时间打断：body 不完整。有些源连得上、但一个字节一个字节往回滴，
    # 每次 read 都在超时内返回一点点，光靠 socket 超时永远判不出它已经废了。
    truncated: bool = False

    @property
    def content_type(self) -> str:
        if not self.headers:
            return ""
        return (self.headers.get("content-type") or "").lower()

    @property
    def size(self) -> int:
        return len(self.body)

    @property
    def speed_mbps(self) -> float:
        """按「传输阶段」计算速率，单位 Mbps。"""
        if self.elapsed_ms <= 0 or not self.body:
            return 0.0
        seconds = self.elapsed_ms / 1000.0
        return round(len(self.body) * 8 / seconds / 1_000_000, 2)

    @property
    def text(self) -> str:
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return ""


class HttpClient:
    """带重试、限长读取的轻量客户端。"""

    def __init__(
        self,
        user_agent: str = "",
        timeout: float = 8.0,
        retries: int = 1,
        proxy: str = "",
    ) -> None:
        self.user_agent = user_agent or DEFAULT_UA
        self.timeout = timeout
        self.retries = max(1, retries)
        self.proxy = proxy
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------ #
    def get(
        self,
        url: str,
        *,
        limit: int = 0,
        headers: dict | None = None,
        allow_redirect: bool = True,
        deadline: float | None = None,
    ) -> Response:
        """读取 URL。limit > 0 时只读到 limit 字节就断开（用于测速）。

        ``deadline`` 是 ``time.monotonic()`` 口径的绝对截止时刻：连接、首字节、
        读体加起来不能超过它。慢滴流的源靠 socket 超时判不出来（每 8 秒挤一点，
        永远不超时），必须给它一个总时限。
        """
        last = Response(url=url, error="未执行")
        for attempt in range(self.retries):
            last = self._get_once(
                url, limit=limit, headers=headers, allow_redirect=allow_redirect, deadline=deadline
            )
            if last.truncated:
                return last  # 慢源不重试：重试只是把同样的等待再做一遍
            if last.status and last.status < 400 and last.body:
                return last
            if last.status in (401, 403, 404, 410):
                return last  # 明确的拒绝不必重试
            if deadline is not None and time.monotonic() >= deadline:
                return last  # 已经到点，别把重试的时间也搭进去
            if attempt + 1 < self.retries:
                time.sleep(0.4 * (attempt + 1))
        return last

    # ------------------------------------------------------------------ #
    def _op_timeout(self, deadline: float | None) -> float:
        """单次 socket 操作的超时：不超过配置值，也不越过截止时刻。"""
        if deadline is None:
            return self.timeout
        left = deadline - time.monotonic()
        return max(0.05, min(self.timeout, left))

    # ------------------------------------------------------------------ #
    def _get_once(
        self,
        url: str,
        *,
        limit: int,
        headers: dict | None,
        allow_redirect: bool,
        deadline: float | None = None,
    ) -> Response:
        resp = Response(url=url)
        current = url
        for _ in range(_MAX_REDIRECTS if allow_redirect else 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https"):
                resp.error = f"不支持的协议: {parts.scheme}"
                return resp

            hdrs = {
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            if headers:
                hdrs.update(headers)
            if limit > 0:
                hdrs["Range"] = f"bytes=0-{limit - 1}"

            started = time.monotonic()
            try:
                conn = self._connect(parts, deadline)
                if self.proxy and parts.scheme == "http":
                    target = current  # 走代理时 http 请求目标用绝对地址
                else:
                    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
                conn.request("GET", target, headers=hdrs)
                raw = conn.getresponse()

                resp.status = raw.status
                resp.headers = {k.lower(): v for k, v in raw.getheaders()}
                resp.url = current
                resp.ttfb_ms = int((time.monotonic() - started) * 1000)

                location = resp.headers.get("location")
                if raw.status in (301, 302, 303, 307, 308) and location and allow_redirect:
                    raw.read(0)
                    conn.close()
                    current = _join(current, location)
                    continue

                # 首字节回来可能已经用掉大半个截止时间，读体按剩下的时间收紧
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(self._op_timeout(deadline))
                    except OSError:
                        pass
                body_started = time.monotonic()
                resp.body, resp.truncated = self._read_body(raw, limit, deadline)
                resp.elapsed_ms = int((time.monotonic() - body_started) * 1000)
                if resp.truncated:
                    resp.error = "读取超时"
                conn.close()
                return self._maybe_inflate(resp)

            except Exception as exc:  # noqa: BLE001 - 网络异常种类繁多
                resp.error = f"{type(exc).__name__}: {exc}"
                resp.status = resp.status or 0
                resp.truncated = resp.truncated or (
                    deadline is not None and time.monotonic() >= deadline
                )
                return resp

        resp.error = resp.error or "重定向次数过多"
        return resp

    # ------------------------------------------------------------------ #
    def _connect(self, parts, deadline: float | None = None):
        import http.client

        timeout = self._op_timeout(deadline)
        proxy_host, _, proxy_port = self.proxy.partition(":")
        use_proxy = bool(self.proxy)

        if parts.scheme == "https":
            if use_proxy:
                conn = http.client.HTTPSConnection(
                    proxy_host, int(proxy_port or 8080), timeout=timeout, context=self._ssl
                )
                conn.set_tunnel(parts.hostname, parts.port or 443)
                return conn
            return http.client.HTTPSConnection(
                parts.hostname, parts.port or 443, timeout=timeout, context=self._ssl
            )
        if use_proxy:
            return http.client.HTTPConnection(proxy_host, int(proxy_port or 8080), timeout=timeout)
        return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)

    @staticmethod
    def _read_body(raw, limit: int, deadline: float | None = None) -> tuple[bytes, bool]:
        """读响应体，返回 ``(内容, 是否被截止时间截断)``。

        只按字节数停是不够的：慢滴流每次 read 都能拿到一点，永远到不了 limit。
        所以每轮都看一眼总时间，到点就走人。
        """
        chunk = 16384
        buf = bytearray()
        while True:
            if limit > 0 and len(buf) >= limit:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return bytes(buf), True
            try:
                piece = raw.read(chunk if limit <= 0 else min(chunk, limit - len(buf)))
            except Exception:  # noqa: BLE001 - 直播流常在读中途被截断
                break
            if not piece:
                break
            buf.extend(piece)
        if deadline is not None and time.monotonic() >= deadline:
            return bytes(buf), True
        return bytes(buf), False

    @staticmethod
    def _maybe_inflate(resp: Response) -> Response:
        encoding = (resp.headers or {}).get("content-encoding", "").lower()
        if encoding == "gzip" and resp.body:
            try:
                resp.body = gzip.decompress(resp.body)
            except Exception:  # noqa: BLE001
                pass
        return resp


def _join(base: str, location: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, location)


def build_client(
    user_agent: str = "",
    timeout: float = 8.0,
    retries: int = 1,
    proxy: str = "",
) -> HttpClient:
    return HttpClient(user_agent=user_agent, timeout=timeout, retries=retries, proxy=proxy)


__all__ = ["HttpClient", "Response", "build_client", "DEFAULT_UA", "__version__"]
