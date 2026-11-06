"""播放清单改写：由本机用正确的请求头取清单，分片交给播放器直连。

有些 CDN 只放行自家客户端的 User-Agent。HTTP 的 m3u 语法里虽然有
``#EXTVLCOPT:http-user-agent=``，但播放器请求 HLS 的子清单和分片时会换成
自己的 UA，于是主清单能打开、画面却出不来——表现就是"列表里只有一个台能播"。

**所以清单必须由本机取**（用上游认的 UA）。但**分片一定要让播放器直连 CDN**，
不能经本机转发——这是实测出来的关键差别：同一条流，全部经本机代理时，本机去
拉分片会被平台回 403（每片一次裸请求，被判成异常流量），而只在清单这一跳经本机、
分片直连时，任何播放器都能稳定播。区别只在于「谁去拉分片」：播放器连着 TLS
会话、带 keep-alive，本机代理则是几百个孤立的短连接。

默认因此是 ``direct`` 模式：改写清单里的每一行成**上游绝对地址**，播放器直连。
少数播放器（或 CDN 对来源 IP 挑剔时）需要旧的全代理行为，把模式切成 ``proxy``
即可——那时的分片才会走 ``/relay/...``。

**为什么播放器会「看十几秒就跳到下一个台」**：播放器把列表里的每一条当成一段
媒体，某一条一旦取不到（4xx/5xx、超时），它就顺延播下一条——于是失败一次等于
跳一个台。上游对同一 IP 的请求频率很敏感，播放器刷新清单加上多客户端同时看，
很容易撞上拒绝。所以这里的策略是「宁可慢一点、也别让播放器看到失败」：

- 清单缓存 5 秒，同一时刻的并发请求共用一次上游请求
- 清单拉取失败时**返回上一份成功的清单**（最多 120 秒），播放器不会跳台
- 分片按内容缓存（同一片不会变），重复请求、拖动、多客户端都不再打上游（仅 proxy 模式）
- 分片被拒时先等 250ms 重试一次，仍失败才报错——分片失败播放器只会重试这一片，
  不会跳到下一个台，所以这里可以如实报错
- 地址拿到后一直用，只有上游真的拒绝（401/403/404/410）才换新的，且换地址有
  45 秒冷却，避免失败时把接口敲爆

安全：proxy 模式下中转目标只允许上游自己的域名，避免变成一个可以访问任意地址的代理。
"""

from __future__ import annotations

import base64
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

# 允许中转的域名后缀。只放行上游自己的 CDN，不做通用代理。
ALLOW_SUFFIX = (".cctv.cn", ".cctv.com", ".ysp.cctv.cn", ".cztv.com", ".cztvcloud.com")

# m3u8 里的 URI 属性（#EXT-X-KEY / #EXT-X-MAP / #EXT-X-MEDIA 等）
_URI_ATTR = re.compile(r'URI="([^"]+)"')

# 视为「地址已失效」的状态码
_GONE = (401, 403, 404, 410)

# 分片被拒时，重试前的等待
_RETRY_PAUSE = 0.25

# 取清单/分片时带的 Referer
_REFERER = "https://live.cctv.cn/"


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


def parse_range(spec: str, size: int) -> tuple[int, int] | None:
    """解析 ``bytes=start-end``。不支持/非法返回 None（按整段返回）。"""
    if not spec or not spec.lower().startswith("bytes=") or "," in spec:
        return None
    body = spec.split("=", 1)[1].strip()
    start_s, _, end_s = body.partition("-")
    try:
        if start_s == "":
            # bytes=-N：最后 N 字节
            length = int(end_s)
            if length <= 0:
                return None
            start = max(0, size - length)
            return start, size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start < 0:
        return None
    return start, min(end, size - 1)


class RelayError(Exception):
    """中转失败：上游不可达、被拒、或目标不在放行名单内。"""


class _SegmentCache:
    """按字节数限额的 LRU。分片内容不会变，缓存是安全的。"""

    def __init__(self, max_bytes: int, ttl: float) -> None:
        self.max_bytes = max(0, max_bytes)
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[float, str, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[str, bytes] | None:
        if self.max_bytes <= 0:
            return None
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                self.misses += 1
                return None
            at, ctype, body = hit
            if now - at > self.ttl:
                self._drop(key)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return ctype, body

    def put(self, key: str, ctype: str, body: bytes) -> None:
        if self.max_bytes <= 0 or not body or len(body) > self.max_bytes:
            return
        with self._lock:
            if key in self._data:
                self._drop(key)
            self._data[key] = (time.time(), ctype, body)
            self._bytes += len(body)
            while self._bytes > self.max_bytes and self._data:
                self._drop(next(iter(self._data)))

    def _drop(self, key: str) -> None:
        item = self._data.pop(key, None)
        if item:
            self._bytes -= len(item[2])

    def resize(self, max_bytes: int) -> None:
        """改限额，并把超出部分从最旧的开始丢掉。"""
        with self._lock:
            self.max_bytes = max(0, int(max_bytes))
            while self._bytes > self.max_bytes and self._data:
                self._drop(next(iter(self._data)))

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "bytes": self._bytes}


class Relay:
    """按频道缓存上游地址，并把清单改写成播放器能直接用的形式。

    ``resolver(slug)`` 应该返回 ``(playurl, 该流需要的 UA)``；调用方可传
    自己的实现，测试里可以塞假的。

    ``mode``:

    ``direct``  清单里的地址换成上游绝对地址，分片由播放器直连 CDN（默认，最流畅）
    ``proxy``   分片也经本机转发，只在播放器直连放不出来时用
    """

    def __init__(
        self,
        resolver,
        *,
        timeout: float = 8.0,
        seg_timeout: float = 20.0,
        retry_gap: float = 20.0,
        playlist_ttl: float = 1.0,
        addr_ttl: float = 300.0,
        stale_max: float = 120.0,
        cache_bytes: int = 96 * 1024 * 1024,
        cache_ttl: float = 300.0,
        mode: str = "direct",
    ):
        self._resolver = resolver
        self._mode = mode if mode in ("direct", "proxy") else "direct"
        self._timeout = timeout
        self._seg_timeout = seg_timeout
        self._retry_gap = retry_gap
        self._playlist_ttl = playlist_ttl
        self._addr_ttl = addr_ttl
        self._stale_max = stale_max
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str, str]] = {}   # slug -> (时间, 地址, UA)
        self._failed: dict[str, float] = {}                   # slug -> 上次换地址失败的时间
        self._errors: dict[str, str] = {}                     # slug -> 最近一次错误
        self._playlists: dict[str, tuple[float, str]] = {}    # slug -> (时间, 已改写的清单)
        self._flights: dict[str, threading.Lock] = {}         # slug -> 拉清单的去重锁
        self._segments = _SegmentCache(cache_bytes, cache_ttl)
        self.failures = 0                                     # 累计失败次数
        self.stale_served = 0                                 # 用旧清单兜住的次数
        # 出口级限流：上游按 IP 限流，一旦被限，所有频道会一起 403。
        # 这时继续换票只会把限流拖得更长，所以短暂拦住「没有缓存地址」的频道。
        self._forbidden_hits = 0
        self._forbidden_until = 0.0
        # 出口预算（令牌桶）：正常播放每个台 5 秒才要一次清单，很闲；但播放器
        # 刷新订阅、检测可用性时会几十秒内把每个台的 /live/xxx.m3u8 全 GET 一遍，
        # 一次几十个请求打上去，正好撞上游的按 IP 限频——被限之后连正在看的人
        # 都会一起 403。这里把「向上游要清单」的总速率卡住，超出就先用缓存顶着。
        self._rate = 1.2          # 每秒最多向上游要 1.2 次清单
        self._burst = 10.0        # 允许的瞬时突发
        self._tokens = self._burst
        self._tokens_at = time.monotonic()
        self._bucket_lock = threading.Lock()
        self.throttled = 0        # 因预算不足而没去上游的次数

    def _take_token(self, wait: float = 4.0) -> bool:
        """取一个出口配额，等不到就返回 False（调用方用缓存顶住）。"""
        deadline = time.monotonic() + wait
        while True:
            with self._bucket_lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst, self._tokens + (now - self._tokens_at) * self._rate
                )
                self._tokens_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                need = (1.0 - self._tokens) / self._rate
            if time.monotonic() + need > deadline:
                return False
            time.sleep(min(need, 0.25))

    # ---------------- 上游 ----------------
    def upstream(self, slug: str, *, fresh: bool = False) -> tuple[str, str]:
        """取该频道当前可用的上游地址与 UA。

        ``fresh=True`` 表示「刚被上游拒绝，想换一个」，受冷却时间约束：
        短时间内重复失败只试一次，避免把接口敲到限流。
        """
        now = time.time()
        with self._lock:
            hit = self._cache.get(slug)
            cooling = now - self._failed.get(slug, 0.0) < self._retry_gap
            blocked = now < self._forbidden_until

        if hit and not fresh:
            # 地址用久了要主动换票。CDN 对同一张票据上的重复请求会越来越容易
            # 回 403（换一个 token 就正常），所以别一直抱着同一张用。
            if now - hit[0] <= self._addr_ttl:
                return hit[1], hit[2]
            fresh = True

        if cooling:
            # 冷却期内不再敲门：有旧地址就继续用，没有就把上次的错抛出去
            if hit:
                return hit[1], hit[2]
            raise RelayError(self._errors.get(slug, "上游暂时不可用，稍后自动重试"))

        if blocked and not hit:
            # 整条出口正被上游限流：这个台连地址都没有，硬敲只会让所有人更久播不了
            raise RelayError("上游限流中（403），等十几秒再换台")

        try:
            url, ua = self._resolver(slug)
        except Exception as exc:  # noqa: BLE001 - 解析失败要能兜住
            message = str(exc) or type(exc).__name__
            with self._lock:
                self._failed[slug] = now
                self._errors[slug] = message
                self.failures += 1
                stale = self._cache.get(slug)
            if stale:
                return stale[1], stale[2]
            raise RelayError(message) from exc

        if not url:
            with self._lock:
                self._failed[slug] = now
                self._errors[slug] = "解析结果为空"
            raise RelayError(f"{slug} 没有解析出可用地址")

        with self._lock:
            self._cache[slug] = (now, url, ua)
            self._failed.pop(slug, None)
            self._errors.pop(slug, None)
        return url, ua

    def forget(self, slug: str) -> None:
        """丢掉地址与清单缓存，下次请求时换新地址。"""
        with self._lock:
            self._cache.pop(slug, None)
            for key in [k for k in self._playlists if k.endswith(f"|{slug}")]:
                self._playlists.pop(key, None)

    def configure(
        self,
        *,
        playlist_ttl: float | None = None,
        cache_bytes: int | None = None,
        mode: str | None = None,
    ) -> None:
        """面板上改了参数后立刻生效，不用重启容器。

        清单缓存调小=更实时但更费上游请求；调大=更抗限流但换台慢半拍。
        切模式要把已缓存的清单清掉，否则播放器还会拿到旧写法。
        """
        if playlist_ttl is not None:
            self._playlist_ttl = max(0.5, float(playlist_ttl))
        if cache_bytes is not None:
            self._segments.resize(int(cache_bytes))
        if mode is not None and mode in ("direct", "proxy") and mode != self._mode:
            self._mode = mode
            with self._lock:
                self._playlists.clear()

    def _mark_failed(self, slug: str, message: str) -> None:
        """记一次失败并进入冷却。

        冷却不只是防接口被敲爆：一个频道的分片请求是并发来的，失败时若不加
        冷却，每个分片都会触发一次重新解析，几十个请求瞬间堆上去。

        **注意这里不丢地址缓存**。早先的实现顺手把地址删了，结果一次分片 403
        就会导致下一次清单请求无地址可用 → 解析又撞限流 → 返回 502 →
        播放器立刻跳到下一个频道。上游的 403 大多是 IP 级限流，换个地址也
        一样被拒，留着旧地址反而能继续用。
        """
        with self._lock:
            self._failed[slug] = time.time()
            self._errors[slug] = message
            self.failures += 1
            if "403" in message:
                # 连续三个频道都被 403，基本可以认定是本机出口被限流，
                # 而不是某个台单独坏了。停 20 秒，让上游的计数缓一缓。
                self._forbidden_hits += 1
                if self._forbidden_hits >= 3:
                    self._forbidden_until = time.time() + 20.0
                    self._forbidden_hits = 0
            else:
                self._forbidden_hits = 0

    def stats(self) -> dict:
        """给面板看的简况。"""
        now = time.time()
        with self._lock:
            cached = {k: round(now - v[0]) for k, v in self._cache.items()}
            playlists = {k.split("|", 1)[-1]: round(now - v[0], 1) for k, v in self._playlists.items()}
        seg = self._segments.stats()
        seg.update({"hits": self._segments.hits, "misses": self._segments.misses})
        return {
            "mode": self._mode,
            "cached": cached,
            "playlists": playlists,
            "segments": seg,
            "errors": dict(self._errors),
            "failures": self.failures,
            "stale_served": self.stale_served,
            # 出口预算用光的次数：说明播放器在批量扫台，不是故障
            "throttled": self.throttled,
            # 出口被限流时，没缓存的台会被直接挡回来，面板据此提示等一下
            "blocked_for": max(0, round(self._forbidden_until - now)),
        }

    def _fetch(self, url: str, ua: str, *, timeout: float | None = None) -> tuple[int, dict, bytes]:
        # Referer 不能省：官方 CDN 对缺 Referer 的请求更容易回 403
        headers = {"User-Agent": ua or "qqlive", "Referer": _REFERER}
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read() or b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RelayError(f"上游不可达：{exc}") from exc

    # ---------------- 改写 ----------------
    def wrap(self, slug: str, url: str, prefix: str = "") -> str:
        """把上游地址换成本机的转发地址（仅 proxy 模式）。

        ``prefix`` 是这次的对外地址（比如 ``http://192.168.1.10:9797``）。
        给全路径而不是 ``/relay/...``：少数播放器不按清单地址解析相对路径，
        拿到相对地址就直接报「格式不支持」。
        """
        return f"{prefix}/relay/{urllib.parse.quote(slug)}/p/{encode(url)}"

    def target(self, slug: str, url: str, prefix: str = "") -> str:
        """这一行最终写给播放器的地址。

        direct 模式直接给上游绝对地址（分片由播放器直连 CDN，最流畅）；
        proxy 模式换成 ``/relay/...``，由本机转手。
        """
        if self._mode == "direct":
            return url
        return self.wrap(slug, url, prefix)

    def _rewrite(self, text: str, base: str, slug: str, prefix: str = "") -> str:
        out: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                out.append(line)
                continue
            if stripped.startswith("#"):
                out.append(
                    _URI_ATTR.sub(
                        lambda m: f'URI="{self.target(slug, urllib.parse.urljoin(base, m.group(1)), prefix)}"',
                        line,
                    )
                )
            else:
                out.append(self.target(slug, urllib.parse.urljoin(base, stripped), prefix))
        return "\n".join(out) + "\n"

    # ---------------- 对外 ----------------
    def playlist(self, slug: str, prefix: str = "") -> str:
        """返回改写后的 m3u8 文本。

        **每次都向上游要一份新的，不缓存正文。** 上游清单只有 3 个分片、5 秒滚
        一片，把同一份正文发给播放器第二遍，等于让它反复播同一批分片：播完这十几
        秒就没有下一片，而那批分片早被 CDN 回收，重取只会 403——表现就是「看着
        看着卡住」「十几秒跳台」。所以这里只缓存入口地址，正文每次重取；同一瞬间
        的并发（主清单 + 子清单、多台设备同看一个台）合并成一次刷新。

        失败时返回上一份成功的清单：播放器拿不到清单会直接跳到下一个频道，比给它
        一份稍旧（但结构完整）的清单糟得多。
        """
        key = f"{prefix}|{slug}"
        with self._lock:
            cached = self._playlists.get(key)
            # 去抖窗口只用来合并「同一批分片」的并发请求，不用于跨刷新复用
            if cached and time.time() - cached[0] <= self._playlist_ttl:
                return cached[1]
            flight = self._flights.setdefault(key, threading.Lock())

        # 同一频道的并发刷新只让一个去上游，其余等它把结果放进来
        with flight:
            with self._lock:
                cached = self._playlists.get(key)
                if cached and time.time() - cached[0] <= self._playlist_ttl:
                    return cached[1]

            try:
                # 有旧清单的台：配额不够就直接交旧的，播放器不会卡在那等。
                # 从没成功过的台：多等一会儿，让它排队拿到一次真清单，
                # 否则播放器会把「暂时没排上」当成这个台坏了。
                wait = 4.0 if cached else 15.0
                if not self._take_token(wait):
                    self.throttled += 1
                    raise RelayError("正在批量请求频道，稍后自动恢复")
                text = self._fetch_playlist(slug, prefix)
            except RelayError:
                with self._lock:
                    stale = self._playlists.get(key)
                if stale and time.time() - stale[0] <= self._stale_max:
                    self.stale_served += 1
                    return stale[1]
                raise

            with self._lock:
                self._playlists[key] = (time.time(), text)
            return text

    def _fetch_playlist(self, slug: str, prefix: str = "") -> str:
        url, ua = self.upstream(slug)
        status, _, body = self._fetch(url, ua)
        if status in _GONE:
            # 地址失效或被限流：换一个新地址再试一次。中间停半秒——
            # 403 多半是出口级限频，连着两次贴脸请求只会被拒得更狠。
            time.sleep(0.4)
            url, ua = self.upstream(slug, fresh=True)
            status, _, body = self._fetch(url, ua)
        if status != 200 or not body:
            self._mark_failed(slug, f"上游返回 {status}")
            raise RelayError(f"上游返回 {status}，取不到播放清单")

        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text:
            self._mark_failed(slug, "上游返回的不是 m3u8 清单")
            raise RelayError("上游返回的不是 m3u8 清单")
        return self._rewrite(text, url, slug, prefix)

    def resource(
        self, slug: str, token: str, range_spec: str = "", prefix: str = ""
    ) -> tuple[int, str, bytes, dict]:
        """转发一个分片 / 密钥 / 子清单。

        返回 ``(状态码, Content-Type, 内容, 额外响应头)``；``range_spec`` 是
        播放器发来的 ``Range`` 头，支持的话按它返回 206。
        """
        url = decode(token)
        if not url:
            raise RelayError("转发地址无法解析")
        if not allowed(url):
            raise RelayError("该地址不在允许中转的范围内")

        # 清单类的地址会随直播推进而变化（上面带 svrtime），不能长期缓存；
        # 分片地址一一对应固定内容，可以放心缓存。
        is_playlist_url = urllib.parse.urlsplit(url).path.lower().endswith((".m3u8", ".m3u"))
        if not is_playlist_url:
            hit = self._segments.get(url)
            if hit is not None:
                return self._slice(*hit, range_spec)

        # slug 只用来做失效清理，未知频道也能转（分片地址自带上游签名）
        try:
            _, ua = self.upstream(slug)
        except RelayError:
            ua = "qqlive"

        status, headers, body = self._fetch(url, ua, timeout=self._seg_timeout)
        if status in _GONE:
            # 上游偶发拒绝很常见，稍等一下再试一次，通常就过了
            time.sleep(_RETRY_PAUSE)
            status, headers, body = self._fetch(url, ua, timeout=self._seg_timeout)
        if status in _GONE:
            self._mark_failed(slug, f"分片被拒（{status}）")
            raise RelayError(f"上游返回 {status}")
        if status != 200:
            raise RelayError(f"上游返回 {status}")

        # 子清单也是 m3u8，里面的分片地址同样要换成走本机，否则播放器会
        # 拿着上游地址直连，又回到被 UA 拒绝的老问题
        if _is_playlist(url, headers, body):
            text = body.decode("utf-8", "replace")
            rewritten = self._rewrite(text, url, slug, prefix).encode("utf-8")
            return 200, "application/vnd.apple.mpegurl; charset=utf-8", rewritten, {}

        ctype = headers.get("Content-Type") or _guess_type(url)
        self._segments.put(url, ctype, body)
        return self._slice(ctype, body, range_spec)

    @staticmethod
    def _slice(ctype: str, body: bytes, range_spec: str) -> tuple[int, str, bytes, dict]:
        if range_spec:
            span = parse_range(range_spec, len(body))
            if span is not None:
                start, end = span
                return (
                    206,
                    ctype,
                    body[start : end + 1],
                    {"Content-Range": f"bytes {start}-{end}/{len(body)}"},
                )
            if "," not in range_spec:
                # 单段范围但不可满足：如实回 416，播放器会改用整段请求
                return 416, ctype, b"", {"Content-Range": f"bytes */{len(body)}"}
            # 多段范围（极少见）不支持，直接给整段，比回 416 安全
        return 200, ctype, body, {}


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


__all__ = ["Relay", "RelayError", "allowed", "encode", "decode", "parse_range"]
