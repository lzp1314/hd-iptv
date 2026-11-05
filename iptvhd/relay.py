"""本地中转：把需要特殊请求头的 HLS 清单和分片转给播放器。

有些 CDN 只放行自家客户端的 User-Agent。HTTP 的 m3u 语法里虽然有
``#EXTVLCOPT:http-user-agent=``，但播放器请求 HLS 的子清单和分片时会换成
自己的 UA，于是主清单能打开、画面却出不来——表现就是"列表里只有一个台能播"。

中转层用正确的 UA 向上游取，把 m3u8 里的每一行改写成 ``/relay/...`` 形式的
本地地址，分片也是本机转手。播放器只跟本机说话，UA 不再是问题；地址临近
过期时，播放器下一次刷新清单会拿到新换的地址，过期问题也一并消失。

安全：中转目标只允许上游自己的域名，避免变成一个可以访问任意地址的代理。
"""

from __future__ import annotations

import base64
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 允许中转的域名后缀。只放行上游自己的 CDN，不做通用代理。
ALLOW_SUFFIX = (".cctv.cn", ".cctv.com", ".ysp.cctv.cn", ".cztv.com", ".cztvcloud.com")

# m3u8 里的 URI 属性（#EXT-X-KEY / #EXT-X-MAP / #EXT-X-MEDIA 等）
_URI_ATTR = re.compile(r'URI="([^"]+)"')


def allowed(url: str) -> bool:
    """目标地址是否在放行名单内。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    for suffix in ALLOW_SUFFIX:
        bare = suffix.lstrip(".")
        if host == bare or host.endswith(suffix):
            return True
    return False


def encode(url: str) -> str:
    """把目标地址编成 URL 安全的一段。"""
    raw = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")


def decode(token: str) -> str:
    """还原目标地址，非法输入返回空串。"""
    pad = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


class RelayError(Exception):
    """中转失败：上游不可达、被拒、或目标不在放行名单内。"""


class Relay:
    """按频道缓存上游地址，并把清单改写成指向本机的地址。

    ``resolver(slug)`` 应该返回 ``(playurl, 该流需要的 UA)``；调用方可传
    自己的实现，测试里可以塞假的。
    """

    def __init__(self, resolver, *, ttl: float = 180.0, timeout: float = 12.0):
        self._resolver = resolver
        self._ttl = ttl
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str, str]] = {}  # slug -> (时间, 地址, UA)

    # ---------------- 上游 ----------------
    def upstream(self, slug: str, *, fresh: bool = False) -> tuple[str, str]:
        """取该频道当前可用的上游地址与 UA，过期或要求强制刷新时重新解析。"""
        now = time.time()
        with self._lock:
            hit = self._cache.get(slug)
            if hit and not fresh and now - hit[0] < self._ttl:
                return hit[1], hit[2]

        url, ua = self._resolver(slug)
        if not url:
            raise RelayError(f"{slug} 没有解析出可用地址")
        with self._lock:
            self._cache[slug] = (now, url, ua)
        return url, ua

    def forget(self, slug: str) -> None:
        """丢掉缓存，下次请求重新换地址。"""
        with self._lock:
            self._cache.pop(slug, None)

    def _fetch(self, url: str, ua: str) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": ua or "qqlive"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read() or b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RelayError(f"上游不可达：{exc}") from exc

    # ---------------- 改写 ----------------
    def wrap(self, slug: str, url: str) -> str:
        """把上游地址换成本机的转发地址。"""
        return f"/relay/{urllib.parse.quote(slug)}/p/{encode(url)}"

    def _rewrite(self, text: str, base: str, slug: str) -> str:
        out: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                out.append(line)
                continue
            if stripped.startswith("#"):
                out.append(
                    _URI_ATTR.sub(
                        lambda m: f'URI="{self.wrap(slug, urllib.parse.urljoin(base, m.group(1)))}"',
                        line,
                    )
                )
            else:
                out.append(self.wrap(slug, urllib.parse.urljoin(base, stripped)))
        return "\n".join(out) + "\n"

    # ---------------- 对外 ----------------
    def playlist(self, slug: str) -> str:
        """返回改写后的 m3u8 文本。"""
        url, ua = self.upstream(slug)
        status, _, body = self._fetch(url, ua)
        if status in (401, 403, 410):
            # 地址可能已失效，换一个新的再来一次
            url, ua = self.upstream(slug, fresh=True)
            status, _, body = self._fetch(url, ua)
        if status != 200 or not body:
            raise RelayError(f"上游返回 {status}，取不到播放清单")

        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text:
            raise RelayError("上游返回的不是 m3u8 清单")
        return self._rewrite(text, url, slug)

    def resource(self, slug: str, token: str) -> tuple[str, bytes]:
        """转发一个分片 / 密钥，返回 (Content-Type, 内容)。"""
        url = decode(token)
        if not url:
            raise RelayError("转发地址无法解析")
        if not allowed(url):
            raise RelayError("该地址不在允许中转的范围内")

        # slug 只用来做失效清理，未知频道也能转（分片地址自带上游签名）
        try:
            _, ua = self.upstream(slug)
        except (RelayError, KeyError):
            ua = "qqlive"

        status, headers, body = self._fetch(url, ua)
        if status in (403, 404, 410):
            self.forget(slug)
        if status != 200:
            raise RelayError(f"上游返回 {status}")

        # 子清单也是 m3u8，里面的分片地址同样要换成走本机，否则播放器会
        # 拿着上游地址直连，又回到被 UA 拒绝的老问题
        if _is_playlist(url, headers, body):
            text = body.decode("utf-8", "replace")
            return "application/vnd.apple.mpegurl; charset=utf-8", self._rewrite(text, url, slug).encode("utf-8")

        ctype = headers.get("Content-Type") or _guess_type(url)
        return ctype, body


def _is_playlist(url: str, headers: dict, body: bytes) -> bool:
    """判断这次转发的是清单还是分片。"""
    ctype = (headers.get("Content-Type") or "").lower()
    if "mpegurl" in ctype or "m3u" in ctype:
        return True
    if urllib.parse.urlsplit(url).path.lower().endswith((".m3u8", ".m3u")):
        return True
    return body[:7] == b"#EXTM3U"


def _guess_type(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(".m3u8") or path.endswith(".m3u"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".ts"):
        return "video/mp2t"
    if path.endswith(".m4s") or path.endswith(".mp4"):
        return "video/mp4"
    if path.endswith(".aac"):
        return "audio/aac"
    if path.endswith(".key"):
        return "application/octet-stream"
    return "application/octet-stream"


__all__ = ["Relay", "RelayError", "allowed", "encode", "decode"]
