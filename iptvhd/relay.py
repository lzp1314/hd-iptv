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

- 清单正文**每次现取**，不做跨刷新复用；只有同一瞬间的并发合并成一次上游请求
- 清单拉取失败时**返回上一份成功的清单**（最多 120 秒），播放器不会跳台
- 清单「冻住」会被发现并换票重取：同一个 ``#EXT-X-MEDIA-SEQUENCE`` 超过 10 秒
  没往前滚，就不是「刚好没滚」，而是那张带 token 的地址卡住了——这类卡死不报
  错，播放器播完手里的十几秒就定在那儿，只能靠比对序号抓出来
- 分片按内容缓存（同一片不会变），重复请求、拖动、多客户端都不再打上游（仅 proxy 模式）
- 分片被拒时先等 250ms 重试一次，仍失败才报错——分片失败播放器只会重试这一片，
  不会跳到下一个台，所以这里可以如实报错
- 地址拿到后一直用，只有上游真的拒绝（401/403/404/410）才换新的，且换地址有
  45 秒冷却，避免失败时把接口敲爆

安全：proxy 模式下中转目标只允许上游自己的域名，避免变成一个可以访问任意地址的代理。
"""

from __future__ import annotations

import base64
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque

# 允许中转的域名后缀。只放行上游自己的 CDN，不做通用代理。
ALLOW_SUFFIX = (".cctv.cn", ".cctv.com", ".ysp.cctv.cn", ".cztv.com", ".cztvcloud.com")

# m3u8 里的 URI 属性（#EXT-X-KEY / #EXT-X-MAP / #EXT-X-MEDIA 等）
_URI_ATTR = re.compile(r'URI="([^"]+)"')

# 清单滚到第几片了。同一个地址反复取却一直是这个数，就说明清单冻住了
_SEQ_LINE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)")

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


# ---------------- 分片滑动窗口 ----------------
# 上游媒体清单只列最近 3 个分片（约 16 秒），但**能取到的不止 3 片**：清单头的
#   #EXT-QQHLS-SEGMENT_RANGE:1786524272-1786524282
# 说明 CDN 上现存的序号范围是 11 片、约 55 秒，再往前的早就回收了。
#
# 以前这里攒到 18 片（90 秒），首片必然落在留存范围之外——播放器一旦从那儿续播
# 就是一片接一片的 403/404，表现是「播一两分钟卡住、然后跳回去」。攒窗口本身没错，
# 错在攒的比上游留得还久。压到 6 片（约 30 秒）刚好盖住播放器起步要的缓冲
# （hls.js 默认 maxBufferLength 30 秒），又稳稳落在 11 片留存之内。
_WINDOW_SEGMENTS = 6       # 最多保留几片。上限 = 上游 CDN 留存（实测 11 片）的一半，留足安全边际
_FRESH_AGE = 6.0           # 窗口超过这么久没更新，就不再当它是新鲜的（会去上游要一份新的）
                           # 实际取值 = max(这个, 预取间隔 + 2)，见 _fresh_age
_STALE_MAX = 30.0          # 再旧也还能发给播放器：上游一时取不到时先顶着，别让画面断
_PREFETCH_EVERY = 5.0      # 有人在看时，后台每隔这么久替播放器去要一次清单
                           # 上游 5 秒滚一片，取更勤有一半是「同一份清单」的无效请求，
                           # 白白烧出口配额，反过来把自己打成限流
_ACTIVE_FOR = 15.0         # 这么久没人要清单就认为没人看，停止预取
_FROZEN_AFTER = 10.0       # 分片序号这么久没往前滚，判定上游冻住

# 「真的有播放器在看」的判定：同一台两次请求的间隔落在 (MIN, WINDOW] 之间才算。
# 播放器拿到清单后要隔两三秒才会再来（hls.js 按分片时长、libVLC 约 5 秒）；
# 而「刷新预览图 / 检测可用性 / 失败自动换台」式的扫台每台只碰一次，或者毫秒级
# 连打两次——都够不到这个区间。只有判定在播的台才值得后台去刷新，否则扫一遍
# 二十个台就是二十次换票加二十次取清单，直接把出口敲进限流。
_PLAYING_MIN_GAP = 1.0
_PLAYING_WINDOW = 15.0

# 上游主备 CDN 全 403 = 本机出口正被限流。这个台冷却 30 秒、一枪不打上游：
# 播放器失败后是毫秒级连环重试，每次重试都换票 + 试主备，只会把限流越拖越久。
_FORBIDDEN_COOLDOWN = 30.0

# 官方回「应版权方要求，暂停提供直播信号」后，这个台冷却一会儿再问。
# 版权按节目走，一档节目没版权时整档都是这句话，换节目才可能恢复。
_BLACKOUT_COOLDOWN = 180.0
_BLACKOUT = "版权"

# 换票预算：整台实例每分钟最多换这么多张票（只管「冷启动」，续播不受限）。
# 一个人换台怎么也到不了一分钟 20 个新台，而把六十多个公开频道扫一遍会在第
# 21 个被拦住——那正是把出口打进限流的典型动作。桶空时本地直接回错，不打上游。
_TICKET_BUDGET = 20
_TICKET_WINDOW = 60.0


def _parse_media(text: str, base: str):
    """把媒体清单拆成 ``(头部行, 首个分片序号, [(时长, 绝对地址, 时间戳, 断点)])``。

    不是「能安全拼接」的媒体清单就返回 ``None``：主清单（带 EXT-X-STREAM-INF）、
    带加密 / 初始化段的清单，攒窗口都会攒错，这类还是走原来的直接改写。

    ``#EXT-X-DISCONTINUITY``（预告片 / 广告切回正片时必然出现）不再让整份清单
    退化成「不攒窗口的 3 片原文」——那样短片段播完、清单衔接不上时，播放器会
    回跳到缓冲里残留的旧分片重播，就是「又重复放」。这里把断点作为分片前的
    一个标志位保留下来，输出时原位还原，让播放器知道这里要跳一下而不是回跳。
    """
    if "#EXTINF" not in text or "#EXT-X-STREAM-INF" in text:
        return None
    if "#EXT-X-KEY:" in text or "#EXT-X-MAP:" in text:
        return None
    if "#EXT-X-ENDLIST" in text:
        return None

    head: list[str] = []
    segs: list[tuple[float, str, str, bool]] = []
    seq = -1
    duration = 0.0
    stamp = ""
    discontinuity = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1].strip())
            except ValueError:
                seq = -1
            continue
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            stamp = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#EXT-X-DISCONTINUITY"):
            discontinuity = True
            continue
        if line.startswith("#EXTINF"):
            body = line.split(":", 1)[1] if ":" in line else ""
            try:
                duration = float(body.split(",", 1)[0].strip())
            except ValueError:
                duration = 0.0
            continue
        if line.startswith("#"):
            head.append(line)
            continue
        segs.append((duration, urllib.parse.urljoin(base, line), stamp, discontinuity))
        stamp = ""
        discontinuity = False

    if not segs:
        return None
    return head, seq, segs


class _Window:
    """一个频道的分片滑动窗口。

    每次从上游拿到清单就按 ``#EXT-X-MEDIA-SEQUENCE`` 对齐并进去，只保留最近的
    ``_WINDOW_SEGMENTS`` 片。序号接不上（上游跳了好几片）时以新清单为准重来，
    免得窗口里的序号和内容错位——那会让播放器直接报错。
    """

    __slots__ = ("head", "segs", "seq0", "at", "moved_at")

    def __init__(self) -> None:
        self.head: list[str] = []
        self.segs: list[tuple[float, str, str, bool]] = []
        self.seq0 = -1
        self.at = 0.0        # 上次并进清单的时间
        self.moved_at = 0.0  # 上次出现新分片的时间（判断上游有没有冻住）

    def feed(self, head, seq, segs, now: float) -> bool:
        """并进一份新清单，返回是否出现了新分片。"""
        self.at = now
        if head:
            self.head = head

        if not self.segs or seq < 0:
            self.segs = list(segs)
            self.seq0 = max(0, seq)
            self.moved_at = now
            return True

        have = self.seq0 + len(self.segs)   # 已经有到（不含）
        if seq > have:
            # 中间缺了好几片，接不上：直接以新清单为准。序号跳变本身就意味着
            # 内容不连续（预告片切回正片、上游序号重置都走这里），给新窗口第一
            # 片补一个断点标记，让播放器重置解码器，而不是拿旧缓冲里的画面回跳。
            self.segs = list(segs)
            if self.segs:
                duration, uri, stamp, _disc = self.segs[0]
                self.segs[0] = (duration, uri, stamp, True)
            self.seq0 = seq
            self.moved_at = now
            return True

        added = False
        for index, seg in enumerate(segs):
            if seq + index >= have:
                self.segs.append(seg)
                added = True
        if added:
            self.moved_at = now
        while len(self.segs) > _WINDOW_SEGMENTS:
            self.segs.pop(0)
            self.seq0 += 1
        return added

    def text(self) -> str:
        # TARGETDURATION 按**窗口里实际的**分片时长重算。上游那份是「最新 3 片」的
        # 值，我们拼出的窗口里可能有更长的片；报小了播放器会按它算刷新节奏和缓冲，
        # 短于实际片长时容易提前判定「这一片还没好」。
        longest = max((seg[0] for seg in self.segs), default=0.0)
        out: list[str] = []
        for line in self.head:
            if line.startswith("#EXT-X-TARGETDURATION"):
                continue
            if line.startswith("#EXT-QQHLS-SEGMENT_RANGE"):
                # 上游「CDN 上还留着哪些序号」的诊断标签，播放器用不上；
                # 留着反而和新拼出来的窗口不一致。
                continue
            out.append(line)
        if longest > 0:
            out.append(f"#EXT-X-TARGETDURATION:{math.ceil(longest)}")
        out.append(f"#EXT-X-MEDIA-SEQUENCE:{max(0, self.seq0)}")
        for duration, uri, stamp, discontinuity in self.segs:
            if discontinuity:
                out.append("#EXT-X-DISCONTINUITY")
            if stamp:
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
            out.append(f"#EXTINF:{duration:.3f},")
            out.append(uri)
        return "\n".join(out) + "\n"


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
        prefetch_every: float = _PREFETCH_EVERY,
        addr_ttl: float = 300.0,
        stale_max: float = _STALE_MAX,
        cache_bytes: int = 96 * 1024 * 1024,
        cache_ttl: float = 300.0,
        mode: str = "direct",
        warm: bool = True,
    ):
        self._resolver = resolver
        self._mode = mode if mode in ("direct", "proxy") else "direct"
        # 是否启用后台预取。关掉之后就退回「每次请求同步去上游」，只给测试用。
        self._warm = warm
        self._timeout = timeout
        self._seg_timeout = seg_timeout
        self._retry_gap = retry_gap
        # 有人在看时，后台每隔这么久替播放器去上游要一次新清单。
        # 调小=更实时但更费出口配额，调大=省配额但滞后一点。
        self._prefetch_every = max(0.5, float(prefetch_every))
        # 「新鲜的」判定要跟着预取节奏走：预取间隔调大到 10 秒时，窗口本来就
        # 会老到 10 秒，若还按固定 6 秒判，每次请求都被标成「旧的那份」。
        self._fresh_age = max(_FRESH_AGE, self._prefetch_every + 2.0)
        self._addr_ttl = addr_ttl
        self._stale_max = stale_max
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str, str]] = {}   # slug -> (时间, 地址, UA)
        self._failed: dict[str, float] = {}                   # slug -> 上次换地址失败的时间
        self._errors: dict[str, str] = {}                     # slug -> 最近一次错误
        self._playlists: dict[str, tuple[float, str, int]] = {}  # 旧版缓存，保留给 stats 兼容
        self._flights: dict[str, threading.Lock] = {}         # slug -> 拉清单的去重锁
        # 分片窗口（按频道，不按对外地址）：同一个台无论是内网还是反代访问，
        # 都用同一份窗口，不会因为换了个域名访问就从头攒。
        self._windows: dict[str, _Window] = {}
        # 拼不了窗口的清单（主清单 / 加密流）原文放这里，key -> (时间, 原文, 基准地址, 序号)
        self._raw: dict[str, tuple[float, str, str, int]] = {}
        # 最近一次有人要清单的时间。只有还在被观看的台才值得后台去预取，
        # 否则几十个台一起刷就是自己把出口 IP 敲到 403。
        self._active: dict[str, float] = {}
        self._ticker: threading.Thread | None = None
        self._stuck_note: dict[str, float] = {}               # slug -> 上次报告冻住的时间
        self._reticket_at: dict[str, float] = {}              # slug -> 上次为「冻住」换票的时间
        # 每个台上一次请求时刻。用来判断「真的有播放器在看」：两次请求间隔
        # 落在 (_PLAYING_MIN_GAP, _PLAYING_WINDOW] 才算，扫台碰不到这个区间。
        self._hits: dict[str, float] = {}
        # 这个台被上游拒了（主备全 403）→ 冷却到此为止，期间一枪不打上游。
        self._forbidden_by_slug: dict[str, float] = {}
        # 这个台正「应版权方要求停播」→ 冷却到此为止，期间本地直接回同一句话。
        self._blackout: dict[str, float] = {}
        # 换票时刻（只留最近一分钟）：整台实例的取票预算，见 _take_ticket。
        self._ticket_times: deque[float] = deque()
        # 正在后台补货的台：同一个台不重复开线程
        self._refreshing: set[str] = set()
        self._segments = _SegmentCache(cache_bytes, cache_ttl)
        self._notify = None                                   # 日志出口，由 service 接上
        self.failures = 0                                     # 累计失败次数
        self.stale_served = 0                                 # 用旧清单兜住的次数
        # 出口级限流：上游按 IP 限流，一旦被限，所有频道会一起 403。
        # 这时继续换票只会把限流拖得更长，所以进入「退避」：后台预取先停手，
        # 让上游的计数缓一缓；退避时间随连续 403 指数拉长（上限 60 秒），
        # 恢复后自动回落到正常节奏。播放器同步取仍允许，只是拿不到时软着陆重试。
        self._forbidden_hits = 0
        self._forbidden_until = 0.0
        self._backoff_base = 6.0    # 首次退避时长，之后翻倍
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

    def _take_ticket(self) -> bool:
        """取一张「换票」配额：整台实例每分钟 _TICKET_BUDGET 张。

        ``_take_token`` 管的是「向上游要清单」的总速率，这个管的是更贵的那一步：
        调央视频的取票接口。它按出口 IP 风控，被判定成异常流量之后换新票也全是
        403，而且会连累正在看的人。播放器「刷新预览图 / 检测可用性 / 失败换台」
        会把六十多个台挨个点一遍，正好是这个形态，所以必须有硬上限。

        只拦冷启动：已经在播的台地址还在（``_addr_ttl`` 内）根本不走这里。
        """
        now = time.monotonic()
        with self._bucket_lock:
            while self._ticket_times and now - self._ticket_times[0] > _TICKET_WINDOW:
                self._ticket_times.popleft()
            if len(self._ticket_times) >= _TICKET_BUDGET:
                return False
            self._ticket_times.append(now)
            return True

    def _note_hit(self, slug: str) -> bool:
        """记一次清单请求，返回「这个台是否真的有播放器在看」。

        判定靠两次请求的间隔：播放器拿完清单要隔两三秒才会再来；扫台是每台
        碰一次，或者毫秒级连打两次。只有落在这个区间才值得为它开后台刷新——

        * 间隔太短（≤ _PLAYING_MIN_GAP）：同一批并发请求，不是「在播」的证据；
        * 间隔太长（> _PLAYING_WINDOW）：播放器早走了，重新开始看时先按「不在播」
          处理，这一次同步取清单，随后自然进入在播状态。
        """
        now = time.time()
        with self._lock:
            previous = self._hits.get(slug)
            gap = now - previous if previous is not None else 0.0
            self._hits[slug] = now
            playing = _PLAYING_MIN_GAP < gap <= _PLAYING_WINDOW
            if playing:
                self._active[slug] = now
            return playing

    # ---------------- 上游 ----------------
    def upstream(self, slug: str, *, fresh: bool = False) -> tuple[str, str]:
        """取该频道当前可用的上游地址与 UA。

        ``fresh=True`` 表示「刚被上游拒绝，想换一个」，受冷却时间约束：
        短时间内重复失败只试一次，避免把接口敲到限流。

        三道闸，任何一道关着就**一枪不打上游**：

        1. 这个台刚被拒（主备全 403）→ 冷却 30 秒；
        2. 这个台正版权停播 → 冷却到点再问；
        3. 换票预算用完 → 本地直接回错，别把出口 IP 打进限流。
        """
        now = time.time()
        with self._lock:
            hit = self._cache.get(slug)
            cooling = now - self._failed.get(slug, 0.0) < self._retry_gap
            slug_blocked = now < self._forbidden_by_slug.get(slug, 0.0)
            blackout_until = self._blackout.get(slug, 0.0)

        if blackout_until > now:
            # 官方说这个台「应版权方要求暂停提供直播信号」。一档节目没版权时整档
            # 都是这句话，播一次问一次没有任何意义，只会一次张票往外打。
            raise RelayError(self._errors.get(slug, "该频道应版权方要求暂停直播"))

        if hit and not fresh:
            # 地址用久了要主动换票。CDN 对同一张票据上的重复请求会越来越容易
            # 回 403（换一个 token 就正常），所以别一直抱着同一张用。
            if now - hit[0] <= self._addr_ttl:
                return hit[1], hit[2]
            fresh = True

        if slug_blocked:
            # 这个台的主备 CDN 刚刚全都 403：本机出口正在被它限流。播放器失败后
            # 是毫秒级连环重试，每一次都换票 + 试主备，只会把限流越拖越久。
            if hit:
                return hit[1], hit[2]
            raise RelayError("本台线路暂时受限，几秒后自动重试")

        if cooling:
            # 冷却期内不再敲门：有旧地址就继续用，没有就把上次的错抛出去
            if hit:
                return hit[1], hit[2]
            raise RelayError(self._errors.get(slug, "上游暂时不可用，稍后自动重试"))

        # 出口级退避（``_forbidden_until``）**只停后台预取**，不挡这里的同步取：
        # 一个台 403 就把所有台一起关掉，等于别人一个坏台害你看不了 CCTV-1。
        # 真正防「批量扫台把出口敲爆」的是下面那张换票预算，按 IP 记账的口径更准。

        if not hit or fresh:
            # 只有真的要向接口要一张新票时才记预算。抱着旧地址续播不计。
            if not self._take_ticket():
                if hit:
                    return hit[1], hit[2]
                raise RelayError("正在批量请求频道，稍后自动恢复")

        try:
            url, ua = self._resolver(slug)
        except Exception as exc:  # noqa: BLE001 - 解析失败要能兜住
            message = str(exc) or type(exc).__name__
            with self._lock:
                self._failed[slug] = now
                self._errors[slug] = message
                self.failures += 1
                stale = self._cache.get(slug)
                if _BLACKOUT in message:
                    # 版权停播要按节目冷却，而不是按「秒」重试。
                    self._blackout[slug] = now + _BLACKOUT_COOLDOWN
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
            self._windows.pop(slug, None)
            self._raw.pop(slug, None)
            self._active.pop(slug, None)
            self._stuck_note.pop(slug, None)
            self._reticket_at.pop(slug, None)
            self._hits.pop(slug, None)
            self._forbidden_by_slug.pop(slug, None)
            self._blackout.pop(slug, None)
            self._refreshing.discard(slug)
            for key in [k for k in self._playlists if k.endswith(f"|{slug}")]:
                self._playlists.pop(key, None)

    def configure(
        self,
        *,
        prefetch_every: float | None = None,
        cache_bytes: int | None = None,
        mode: str | None = None,
    ) -> None:
        """面板上改了参数后立刻生效，不用重启容器。

        预取间隔调小=更实时但更费上游配额；调大=省配额但换台慢半拍。
        切模式要把已缓存的清单清掉，否则播放器还会拿到旧写法。
        """
        if prefetch_every is not None:
            self._prefetch_every = max(0.5, float(prefetch_every))
            self._fresh_age = max(_FRESH_AGE, self._prefetch_every + 2.0)
        if cache_bytes is not None:
            self._segments.resize(int(cache_bytes))
        if mode is not None and mode in ("direct", "proxy") and mode != self._mode:
            self._mode = mode
            with self._lock:
                self._playlists.clear()
                self._windows.clear()
                self._raw.clear()

    def _mark_failed(self, slug: str, message: str) -> None:
        """记一次失败并进入冷却。

        冷却不只是防接口被敲爆：一个频道的分片请求是并发来的，失败时若不加
        冷却，每个分片都会触发一次重新解析，几十个请求瞬间堆上去。

        **注意这里不丢地址缓存**。早先的实现顺手把地址删了，结果一次分片 403
        就会导致下一次清单请求无地址可用 → 解析又撞限流 → 返回 502 →
        播放器立刻跳到下一个频道。上游的 403 大多是 IP 级限流，换个地址也
        一样被拒，留着旧地址反而能继续用。

        403 分两层：**按台**冷却 30 秒（这个台别再来敲门），同时**全局**指数退避
        （整条出口可能都被限了，后台预取先停手）。只做全局会让一个坏台拖累所有台，
        只做按台又挡不住出口级的限流。
        """
        now = time.time()
        with self._lock:
            self._failed[slug] = now
            self._errors[slug] = message
            self.failures += 1
            if "403" in message:
                # 主备 CDN 都拒了才走到这里：这个台先冷却，期间本地直接回错，
                # 不再为它的每次重试换票 + 试主备（那正是把限流越拖越久的做法）。
                self._forbidden_by_slug[slug] = now + _FORBIDDEN_COOLDOWN
                # 全局退避：第 1 次 6 秒，之后翻倍（12 / 24 / 30 封顶）。只影响
                # 后台预取，播放器同步取照旧，只是拿不到时软着陆用旧清单顶着。
                self._forbidden_hits += 1
                self._forbidden_until = now + min(
                    self._backoff_base * (2 ** (self._forbidden_hits - 1)), 30.0
                )
            else:
                self._forbidden_hits = 0

    def stats(self) -> dict:
        """给面板看的简况。"""
        now = time.time()
        with self._lock:
            cached = {k: round(now - v[0]) for k, v in self._cache.items()}
            playlists = {k: round(now - v.at, 1) for k, v in self._windows.items()}
            playlists.update({k: round(now - v[0], 1) for k, v in self._raw.items()})
            active = sorted(self._active)
            cooling = {k: round(v - now) for k, v in self._forbidden_by_slug.items() if v > now}
            blackout = {k: round(v - now) for k, v in self._blackout.items() if v > now}
            tickets = len(self._ticket_times)
        seg = self._segments.stats()
        seg.update({"hits": self._segments.hits, "misses": self._segments.misses})
        return {
            "mode": self._mode,
            "cached": cached,
            "playlists": playlists,
            # 正在被观看、后台在替它预取的台
            "active": active,
            "segments": seg,
            "errors": dict(self._errors),
            "failures": self.failures,
            "stale_served": self.stale_served,
            # 出口预算用光的次数：说明播放器在批量扫台，不是故障
            "throttled": self.throttled,
            # 出口被限流时，没缓存的台会被直接挡回来，面板据此提示等一下
            "blocked_for": max(0, round(self._forbidden_until - now)),
            # 按台冷却（秒）：主备 CDN 全 403 的台，期间一枪不打上游
            "cooling": cooling,
            # 版权停播冷却（秒）：官方明确回「暂停提供直播信号」
            "blackout": blackout,
            # 这一分钟已经用掉的换票额度（上限 _TICKET_BUDGET）
            "tickets": tickets,
            "ticket_budget": _TICKET_BUDGET,
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
        """返回改写后的 m3u8 文本（只要正文）。"""
        return self.playlist_info(slug, prefix)[0]

    def playlist_info(self, slug: str, prefix: str = "") -> tuple[str, bool, int]:
        """返回 ``(清单正文, 是不是旧的那份, 首个分片序号)``。

        播放器每 2~5 秒就要一次清单，这个请求**必须立刻有响应**，绝不能陪着等
        上游——等的那几秒正好是画面断粮的几秒。所以这里的原则是：

        **永远先把手里最新的那份发出去，刷新交给后台。**

        - 窗口是「预取间隔 + 2 秒」（默认 7 秒）内的 → 直接发，等于贴着直播边缘；
          上游 5 秒滚一片，这个新鲜度刚好，不会把已经滚过去的片发给播放器；
        - 略旧但在 ``_STALE_MAX``（30 秒）内 → 也发，标记成「旧的那份」，
          **同时开一个后台线程去补一份新的**。上游一时抽风时这是唯一能让画面
          继续的手段：官方 CDN 留 11 片约 55 秒，30 秒内的片播放器还取得到；
        - 只有冷启动（手上什么都没有）才同步去取。

        **绝不把清单正文按取票有效期缓存**：官方媒体清单只有 3 片、约 16 秒，
        缓存正文等于让播放器在整个缓存期内反复拿到同一批分片——播完这十几秒就
        没有下一片，画面定住；而且那批分片早已被 CDN 回收，重取只会 403。

        另外「在播」是判断出来的（见 ``_note_hit``）：只有真有播放器在轮询的台
        才值得为它开后台刷新。扫台式的每台碰一次不算。
        """
        now = time.time()
        self._note_hit(slug)
        with self._lock:
            window = self._windows.get(slug)
            raw = self._raw.get(slug)
            backing_off = now < self._forbidden_until
        self._ensure_ticker()

        # 新鲜的：直接发。
        if window is not None and window.segs and now - window.at <= self._fresh_age:
            return self._rewrite(window.text(), "", slug, prefix), False, window.seq0
        if raw is not None and now - raw[0] <= self._fresh_age:
            return self._rewrite(raw[1], raw[2], slug, prefix), False, raw[3]

        # 略旧的：还发得出去就先发，别让播放器等。上游留 11 片（约 55 秒），
        # 30 秒内的地址播放器取得到；再旧就得同步换一次了，否则发出去也是死链。
        if window is not None and window.segs:
            age = now - window.at
        elif raw is not None:
            age = now - raw[0]
        else:
            age = float("inf")
        #
        # **发的同时必须在后台补一份新的**。只发不取是个很隐蔽的坑：窗口一旦
        # 过了「新鲜」线而预取又没跟上（后台预取判定这个台没人在看、或者配额
        # 正被批量扫台吃掉），这里就会一直把同一份 3 片清单发给播放器——播完
        # 这十几秒画面定住，下一次请求拿到的还是同一份，序号一动不动。
        if age <= _STALE_MAX:
            cached = self._cached(slug, prefix, stale=True)
            if cached:
                self.stale_served += 1
                self._refresh_async(slug)
                return cached

        # 退避期间：上游正被限流，同步去取基本会被 403，反而让播放器卡在等
        # 上游的几秒上。先把手里的继续发给播放器，让画面接着播。
        if backing_off:
            cached = self._cached(slug, prefix, stale=True)
            if cached:
                self.stale_served += 1
                return cached

        # 手上没得读、或者旧到没意义：同步取一次。
        # 从没成功过的台多等一会儿，让它排上队；有旧数据的台别等太久。
        wait = 4.0 if (window or raw) else 15.0
        if not self._take_token(wait):
            self.throttled += 1
            cached = self._cached(slug, prefix)
            if cached:
                self.stale_served += 1
                return cached
            raise RelayError("正在批量请求频道，稍后自动恢复")

        try:
            text, base = self._fetch_raw(slug)
        except RelayError:
            cached = self._cached(slug, prefix)
            if cached:
                self.stale_served += 1
                return cached
            raise

        self._absorb(slug, text, base)
        ready = self._cached(slug, prefix, stale=False)
        if ready is None:
            raise RelayError("清单里没有分片")
        return ready

    def _refresh_async(self, slug: str) -> None:
        """后台补一份新清单，调用方立刻返回。

        播放器正在等这一份清单，绝不能陪它一起等上游。同一个台同一时刻只允许
        一个补货线程在跑（``_refreshing`` 占位），否则播放器的重试加上并发请求
        会同时开好几个线程去打同一个接口。
        """
        with self._lock:
            if slug in self._refreshing:
                return
            self._refreshing.add(slug)

        def run() -> None:
            try:
                self._prefetch(slug)
            except Exception:  # noqa: BLE001 - 后台补货失败不能影响播放
                pass
            finally:
                with self._lock:
                    self._refreshing.discard(slug)

        threading.Thread(target=run, name=f"hd-iptv-refresh-{slug}", daemon=True).start()

    def _cached(
        self, slug: str, prefix: str, *, stale: bool = True
    ) -> tuple[str, bool, int] | None:
        """把缓存里的那一份改写出来；什么都没有、或者旧到没意义就返回 None。

        ``_STALE_MAX`` 是硬上限：再旧的清单一来播放器取不到片（官方 CDN 只留
        11 片约 55 秒），二来内容早就不是现在播的了，发出去只会让人以为还在播。
        到点就老实报错，让播放器自己重试或者换台。
        """
        now = time.time()
        with self._lock:
            window = self._windows.get(slug)
            raw = self._raw.get(slug)
        if window is not None and window.segs:
            if now - window.at > _STALE_MAX:
                return None
            return self._rewrite(window.text(), "", slug, prefix), stale, window.seq0
        if raw is not None:
            if now - raw[0] > _STALE_MAX:
                return None
            return self._rewrite(raw[1], raw[2], slug, prefix), stale, raw[3]
        return None

    def _absorb(self, slug: str, text: str, base: str) -> None:
        """把一份上游清单并进缓存：能拼窗口的拼窗口，拼不了就存原文。"""
        now = time.time()
        parsed = _parse_media(text, base)
        with self._lock:
            if parsed is None:
                self._raw[slug] = (now, text, base, self._seq_of(text))
                self._windows.pop(slug, None)
                return
            head, seq, segs = parsed
            self._raw.pop(slug, None)
            window = self._windows.get(slug)
            if window is None:
                window = _Window()
                self._windows[slug] = window
            moved = window.feed(head, seq, segs, now)
        if not moved:
            self._note_frozen(slug, window, now)

    def _note_frozen(self, slug: str, window: "_Window", now: float) -> None:
        """上游迟迟不往前滚时写一条运行日志，30 秒最多一条。"""
        if not window.segs:
            return
        idle = now - window.moved_at
        if idle < _FROZEN_AFTER:
            return
        with self._lock:
            last = self._stuck_note.get(slug, 0.0)
            if now - last < 30.0:
                return
            self._stuck_note[slug] = now
        last_seq = window.seq0 + len(window.segs) - 1
        self._note(f"{slug} 的清单停在分片 {last_seq}，已经 {int(idle)} 秒没往前滚")

    # ---------------- 后台预取 ----------------
    def _ensure_ticker(self) -> None:
        if not self._warm:
            return
        with self._lock:
            if self._ticker is not None:
                return
            ticker = threading.Thread(target=self._tick, name="hd-iptv-relay", daemon=True)
            self._ticker = ticker
        ticker.start()

    def _tick(self) -> None:
        while True:
            time.sleep(1.0)
            try:
                self._sweep()
            except Exception:  # noqa: BLE001 - 后台线程不能因为一次异常就没掉
                pass

    def _sweep(self) -> None:
        """给「还在被观看」的台补一次新清单。

        只照顾活跃频道：几十个台一起刷新等于自己把出口 IP 敲到 403，那才是
        真正让人卡的元凶。
        """
        now = time.time()
        with self._lock:
            for slug in [s for s, t in self._active.items() if now - t > _ACTIVE_FOR]:
                self._active.pop(slug, None)
            due = []
            for slug in self._active:
                window = self._windows.get(slug)
                raw = self._raw.get(slug)
                if window is not None:
                    last = window.at
                elif raw is not None:
                    last = raw[0]
                else:
                    last = 0.0
                if now - last >= self._prefetch_every:
                    due.append(slug)
        for slug in due:
            self._prefetch(slug)

    def _prefetch(self, slug: str) -> None:
        """替正在观看的播放器去上游要一份新清单。所有调用方都不用等它。"""
        # 退避期间停手：再敲只会把限流拖得更长。播放器手上的窗口够它再撑几十秒。
        with self._lock:
            backing_off = time.time() < self._forbidden_until
        if backing_off:
            return
        if not self._take_token(0):
            self.throttled += 1
            return

        now = time.time()
        with self._lock:
            window = self._windows.get(slug)
            idle = now - window.moved_at if window is not None and window.segs else 0.0
            last_reticket = self._reticket_at.get(slug, 0.0)

        # 序号 10 秒没滚，基本就是这张票据冻住了，换一张重取。换票也吃配额，
        # 所以 10 秒最多换一次。
        fresh = idle >= _FROZEN_AFTER and now - last_reticket >= _FROZEN_AFTER
        if fresh:
            with self._lock:
                self._reticket_at[slug] = now
        try:
            text, base = self._fetch_raw(slug, fresh=fresh)
        except RelayError:
            return
        self._absorb(slug, text, base)

    @staticmethod
    def _seq_of(text: str) -> int:
        """清单里滚到第几片；没有这个标签（主清单）返回 -1。"""
        match = _SEQ_LINE.search(text or "")
        return int(match.group(1)) if match else -1

    def attach_log(self, notify) -> None:
        """接上一个日志出口，把「清单冻住了」这类事写进面板的运行日志。"""
        self._notify = notify

    def _note(self, message: str) -> None:
        notify = getattr(self, "_notify", None)
        if notify:
            try:
                notify(message)
            except Exception:  # noqa: BLE001 - 记日志不能反过来搞崩播放
                pass

    def _fetch_raw(self, slug: str, *, fresh: bool = False) -> tuple[str, str]:
        """去上游取一份**原始**清单，连它的基准地址一起返回。

        为什么不在这里顺手改写：同一个台可能同时被内网和外网访问，改写结果
        依赖对外地址；存原文、发的时候再改写，一份缓存就够所有人用。
        """
        # 这个台刚被主备 CDN 全 403：冷却期内一枪不打上游。``upstream()`` 在有旧
        # 地址时会「照旧返回」，光靠它挡不住——请求照样会打到上游再被拒一次，
        # 播放器的毫秒级重试叠上来就是一片 403。这里必须直接拦在取清单之前。
        with self._lock:
            cooling_channel = time.time() < self._forbidden_by_slug.get(slug, 0.0)
        if cooling_channel:
            raise RelayError("本台线路暂时受限，几秒后自动重试")

        url, ua = self.upstream(slug, fresh=fresh)
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
        return text, url

    def _fetch_playlist(self, slug: str, prefix: str = "", *, fresh: bool = False) -> str:
        """取一份并改写，不经过窗口缓存。给测试和单次诊断用。"""
        text, base = self._fetch_raw(slug, fresh=fresh)
        return self._rewrite(text, base, slug, prefix)

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
