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
import inspect
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque

from .net import dns_stats

# 允许中转的域名后缀。只放行上游自己的 CDN，不做通用代理。
# miguvideo.com：咪咕最终节点的清单不带 CORS 头、且是 http，浏览器面板直连
# 会被拦，必须让子清单/分片经本机转发（同源下发）。只放行这个域名及其子域。
ALLOW_SUFFIX = (".cctv.cn", ".cctv.com", ".ysp.cctv.cn", ".cztv.com", ".cztvcloud.com",
                ".chinamobile.com", ".miguvideo.com")

# 运营商 IPTV 直播边缘的 IPv6 段（移动 2409 / 联通 2408 / 电信 240e）。这类地址
# 只能按 IP 放行：302 之后上游给的就是裸 IPv6，没有任何域名可以拿去比后缀。
_CARRIER_V6 = ("2409", "2408", "240e")

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

# 直连时可以把分片从 https 降成 http 的域名。
#
# 实测（2026-11-25，NAS 出口）：央视频 CDN 的**同一个分片地址**，https 与 http
# 返回完全相同的字节（2072324 字节，逐字节一致），http 不会被 302 回 https。
#
# 为什么要降：电视盒子（观屿、各类 libVLC 外壳）的 TLS 栈普遍很旧——根证书链
# 不全、只支持到 TLS 1.0/1.1、断电重启后系统时间是错的——播放器直连 https 分片
# 一律报「网络连接错误」。而同一台盒子播 http 的咪咕、运营商源完全正常，说明
# 盒子本身能跑 http。降级之后盒子不必做 TLS 握手，分片仍是它自己直连 CDN，
# 不经过 NAS，因此不会把本机出口打成高频请求（转发正是因此被平台回 403）。
_PLAIN_SUFFIX = (".ysp.cctv.cn",)

# 一张清单票据最多能用几次。**实测（2026-11-25，NAS 出口）：央视频 CDN 的同一个
# 媒体清单地址，第 1、2 次 200，第 3 次就 403**；换一张新票立刻恢复。
#
# 以前只按 ``addr_ttl``（默认 300 秒）复用地址，等于一张票要用到被拒为止：后台
# 每 5 秒取一次清单，第 10 秒必然撞 403，撞完还要退避 1~5 秒。几次叠加就是 20~30
# 秒断档，而窗口总共只有 6 片（约 30 秒）内容——刚好在这段断档里被耗干。于是
# 「播一分多钟卡住、不自恢复」，换台时没有旧窗口可顶就是「黑屏」。
#
# 所以用满 2 次就主动换新票，把「被拒」换成「主动换」：换票次数不变（反正第 3 次
# 一定废），但 403 和不必要的退避都消失了。只对央视频 CDN 生效——运营商源的一张
# 票能拉很多次，跟着换会白白烧掉换票预算。
_TICKET_REUSE = 2


def _is_ysp_url(url: str) -> bool:
    """这个地址是不是央视频 CDN（票据只能用两三次的那一家）。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(_PLAIN_SUFFIX)


def _ipv6_host(url: str) -> str:
    """地址里的主机是 IPv6 字面量就返回它，否则返回空串。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").strip("[]").lower()
    return host if ":" in host else ""


def _carrier_v6(url: str) -> bool:
    """是不是运营商的 IPv6 直播边缘。"""
    host = _ipv6_host(url)
    return bool(host) and host.split(":", 1)[0] in _CARRIER_V6


def allowed(url: str) -> bool:
    """目标地址是否在放行名单内。"""
    if _carrier_v6(url):
        # 运营商 IPTV 边缘只有 IP、没有域名，只能按段放行：限死在 2409/2408/240e，
        # 不然 /relay/<slug>/p/<token> 就成了任意地址都能借道的中转口。
        return True
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


# 静态源的 slug 前缀：``o:`` + base64url(上游地址)。
#
# 为什么把地址编进 slug 而不是维护一张「slug → 地址」的注册表：中转层的一切
# （分片窗口、预取节奏、失效清退）都按 slug 记账，注册表就得跟着结果文件一起
# 重建，进程重启或换一份结果就可能对不上。编进 slug 之后，一个 slug 自带全部
# 信息，重启、重跑、多进程都一致。地址本身是固定的（运营商源不换票），所以
# slug 也固定，窗口能跨更新复用。
#
# 需要这层的只有「上游落在 IPv6 上」的源：官方 IPTV 边缘只发 IPv6 单播，而
# 播放器多半没有 IPv6；让清单和分片都从本机过一手，播放端就与 IPv6 无关了。
STATIC_PREFIX = "o:"

# 普通上游源（聚合源、公开转播）也走中转，用 ``u:`` 区分。
#
# 为什么不能只让「官方源 / IPv6 源」走中转，其余直连：直连的地址是**采集那一刻**
# 从上游拿到的，写进订阅之后就不再变。而聚合源普遍认 UA、按 IP 限流、地址里还
# 带 ``time=`` 这类时间戳——播放器（尤其 ExoPlayer 系）拿到的是一个它自己拼不出来
# 的请求，撞上校验就回 403，表现就是「列表能刷出来，点进去源服务器返回错误」。
# 列表里的 ``#EXTVLCOPT:http-user-agent`` 只有 VLC 认，ExoPlayer 直接当注释跳过。
#
# 改成本机取清单之后，用的是采集阶段就验证过的那套请求头，播放器只需取分片
# （分片绝大多数不校验 UA），「能刷出列表却播不了」这一类就没了。
RELAY_PREFIX = "u:"

# 这类源要的 UA（实测移动的 IPTV 边缘认 okhttp，浏览器 UA 也放行，但用
# 机顶盒同款更稳）
_STATIC_UA = "okhttp/3.12.0"


def needs_relay(url: str) -> bool:
    """这个上游地址是否必须由本机转发（落在 IPv6 上）。"""
    return bool(_ipv6_host(url))


def static_ua() -> str:
    """取官方静态源该用的 UA。

    调用方自己解析线路时（频道级 slug 要按线路决定走哪条）得知道这个值，
    不然会拿浏览器 UA 去打只认机顶盒的运营商边缘。
    """
    return _STATIC_UA


def static_slug(url: str) -> str:
    """为官方源生成 slug（运营商边缘，走机顶盒 UA）。"""
    return STATIC_PREFIX + encode(url)


def plain_slug(url: str) -> str:
    """为普通上游源生成 slug（聚合源等，走采集时的浏览器 UA）。"""
    return RELAY_PREFIX + encode(url)


def static_url(slug: str) -> str:
    """从静态 slug 还原上游地址；``o:`` 与 ``u:`` 两种前缀都认。

    两者只差「取清单时用哪套请求头」，对调用方来说都是「地址编在 slug 里，
    不用注册表」。
    """
    for prefix in (STATIC_PREFIX, RELAY_PREFIX):
        if slug.startswith(prefix):
            return decode(slug[len(prefix):])
    return ""


def slug_ua(slug: str) -> str:
    """这条静态 slug 该用哪套 UA。``u:`` 用浏览器 UA，其余用机顶盒 UA。"""
    if slug.startswith(RELAY_PREFIX):
        from .settings import BROWSER_UA

        return BROWSER_UA
    return _STATIC_UA


# 频道级 slug：``c:`` + base64url(频道名)。
#
# 和静态 slug 的区别在「一个 slug 对应几条线路」。静态 slug 自带一条上游地址，
# 出一条、换一条都得重写列表；而列表里每个台只写一行，那条一旦被上游拒了，
# 播放器就直接报「无法播放」——它手里没有任何备份。
#
# 频道级 slug 只记「是哪个台」，具体用哪条线路交给中转层现场决定：清单取不到
# 就换下一路，播放器这边看到的永远是同一个地址，不需要重新拉订阅。等于把
# 「这个台有好几条源」这件事从列表里挪到了服务端。
CHANNEL_PREFIX = "c:"
# 模块名与频道名的分隔符。选控制字符是因为它不可能出现在配置里的频道名中，
# 也不需要转义；旧 slug 没有这个字符，解析时按「整个都是频道名」兼容。
_MODULE_SEP = "\x1f"


def channel_slug(name: str, module: str = "") -> str:
    """为频道生成频道级 slug。

    ``module`` 是源条目的名字（如「咪咕视频」「运营商官方源」）。同一个台在
    不同模块里各自成组，必须各自用一个地址——否则两条线共用一个 ``c:``
    地址，用户在某个分组里点播却换到了另一个模块的线路，模块隔离就破了。
    """
    key = f"{module}{_MODULE_SEP}{name}" if module else name
    return CHANNEL_PREFIX + encode(key)


def channel_name(slug: str) -> str:
    """从频道级 slug 还原频道名；不是频道级 slug 返回空串。

    带模块前缀的新 slug 只返回频道名部分，模块信息仍完整保留在 ``slug`` 本身
    （线路表按完整 slug 建索引），这里剥掉只是给日志和展示用。
    """
    if not slug.startswith(CHANNEL_PREFIX):
        return ""
    raw = decode(slug[len(CHANNEL_PREFIX):])
    return raw.split(_MODULE_SEP, 1)[-1] if _MODULE_SEP in raw else raw



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
_WINDOW_SEGMENTS = 9       # 最多保留几片。上游 CDN 实测留存 11 片（约 55 秒），
                           # 9 片（约 45 秒）仍留 2 片安全边际。
                           # 为什么不是原来的 6 片：ExoPlayer 的直播起播点默认落后
                           # 3 个 target duration（一片 5~8 秒 → 落后 15~24 秒），
                           # 6 片窗口只剩 6 秒余量，清单断档十几秒就吃干净，画面
                           # 卡死不恢复。9 片把余量抬到 20 秒上下。
_FRESH_AGE = 6.0           # 窗口超过这么久没更新，就不再当它是新鲜的（会去上游要一份新的）
                           # 实际取值 = max(这个, 预取间隔 + 2)，见 _fresh_age
_STALE_MAX = 45.0          # 再旧也还能发给播放器：上游一时取不到时先顶着，别让画面断。
                           # 与窗口总时长对齐（9 片约 45 秒）——顶住的这份本身就是
                           # 45 秒内容，给超过它的年龄没有意义。
_PREFETCH_EVERY = 5.0      # 有人在看时，后台每隔这么久替播放器去要一次清单
                           # 上游 5 秒滚一片，取更勤有一半是「同一份清单」的无效请求，
                           # 白白烧出口配额，反过来把自己打成限流
_ACTIVE_FOR = 15.0         # 这么久没人要清单就认为没人看，停止预取
_FROZEN_AFTER = 10.0       # 分片序号这么久没往前滚，判定上游冻住

# —— 换台预热 ——
# 换到一个「没人看过」的台，第一份清单要现换票再取，两个串行往返约 0.3~0.6 秒，
# 叠加播放器连本机、取首片、解码器初始化，就是换台那 1 秒的来源。这里让中转层
# 提前替「下一个台」把票据和清单取回来，换过去时第一份清单直接命中缓存。
#
# 三条自律（缺一条就会把限流做大）：
# * 必须稳定播够 _WARM_DELAY 才预热——连点换台时每个台都进不了「在看」状态，
#   自然不会预热，而那正是最容易把出口打进限流的时刻；
# * 出口余量不足 _WARM_MIN_TOKENS 直接放弃，不能为了下一个台挤掉在播台的刷新；
# * 预热的清单只在 _WARM_VALID 内有效，过期即弃，不做续期（续期等于全天候盯着
#   两个台刷新，成本翻倍）。
_WARM_DELAY = 8.0
_WARM_VALID = 45.0
_WARM_MIN_GAP = 20.0
_WARM_MIN_TOKENS = 5.0

# 「真的有播放器在看」的判定：同一台两次请求的间隔落在 (MIN, WINDOW] 之间才算。
# 播放器拿到清单后要隔两三秒才会再来（hls.js 按分片时长、libVLC 约 5 秒）；
# 而「刷新预览图 / 检测可用性 / 失败自动换台」式的扫台每台只碰一次，或者毫秒级
# 连打两次——都够不到这个区间。只有判定在播的台才值得后台去刷新，否则扫一遍
# 二十个台就是二十次换票加二十次取清单，直接把出口敲进限流。
_PLAYING_MIN_GAP = 1.0
_PLAYING_WINDOW = 15.0

# 403 之后的重试节奏（秒）：先静默一下、换张新票再取，逐次拉长。
#
# **这是「播十几秒就定住」的直接原因，改这里要小心。** 实测（NAS 上连看 6 分钟）：
# 央视频的媒体清单 URL 只能用几次，同一个地址连取 2~5 次就开始回 403；换一张新票
# 立刻又正常。这本来是几秒钟就能自愈的小抖动，但旧代码按「冷却 30 秒、期间一枪
# 不打上游」处理——窗口里 5 片分片只有约 30 秒，冷却期刚好把画面耗干，于是
# 「播十几秒停住、再等十几秒、跳一下」。30 秒的冷却比 403 本身伤害大得多。
#
# **上限必须短于播放器手里的缓冲**：内置播放器实测只缓冲 8 秒左右（面板上那行
# 「缓冲 8.2s」）。过去序列排到 10、18 秒，一旦退避到第三、四轮，画面必然先耗干
# ——表现就是「播一分多钟转圈九秒、然后定住不动」。所以这里整体压到 5 秒封顶：
# 每一轮都在缓冲撑得住的范围里，403 一恢复立刻就能接上。
_RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0)

# 官方回「应版权方要求，暂停提供直播信号」后，这个台冷却一会儿再问。
# 版权按节目走，一档节目没版权时整档都是这句话，换节目才可能恢复。
_BLACKOUT_COOLDOWN = 180.0
_BLACKOUT = "版权"

# 换票预算：整台实例每分钟最多换这么多张票（只管「冷启动」和「403 换票重取」，
# 抱着旧地址续播不计）。一个台正常播放每分钟只换 1~2 张；被拒时重取一次算一张。
# 把六十多个公开频道扫一遍会在第 31 个被拦住——那正是把出口打进限流的典型动作。
#
# 曾经为了「压限流」降到 20，结果正好卡住正常播放：一个台被 403 打散之后要连着
# 换几次票才接得上，20 张/分钟在中途就见底，换不到票只能干等，画面直接定住。
# 挡住批量扫台真正的闸门是 ScanGuard（按客户端 IP+UA 判定），不是这张总账——
# 总账卡住的是正在看的人。回到 30。
_TICKET_BUDGET = 30
_TICKET_WINDOW = 60.0


class ScanGuard:
    """按客户端拦「批量扫台」，在请求到达上游之前本地回绝。

    实例级换票预算只能看到总账：几台设备各扫几个台，每台都没超自己的节奏，
    加起来照样把 NAS 的出口打进限流。这里按 `IP + UA` 各记一本账：10 秒内
    碰到 6 个不同频道就判定为扫描，之后 30 秒内只允许这个客户端**已经在看**
    的频道继续续播，新频道本地回 429，一枪不打上游。

    正在播的频道不受影响：播放器拿完清单隔两三秒才来取下一份，同一个台
    重复请求不算「碰新台」。扫描通常是每台碰一次、或毫秒级连打两次，都
    判不成在播。
    """

    def __init__(self, limit: int = 6, window: float = 10.0, cooldown: float = 30.0):
        self.limit = max(2, int(limit))
        self.window = max(1.0, float(window))
        self.cooldown = max(1.0, float(cooldown))
        self._lock = threading.Lock()
        self._touches: dict[str, deque] = {}
        self._watching: dict[str, dict[str, float]] = {}
        self._until: dict[str, float] = {}
        self.blocked = 0

    def check(self, client: str, slug: str, now: float | None = None) -> tuple[bool, int]:
        """返回 `(是否放行, 建议重试秒数)`。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            touches = self._touches.setdefault(client, deque())
            watching = self._watching.setdefault(client, {})
            # 只留窗口内的记录；过期就当没碰过。
            while touches and now - touches[0][0] > self.window:
                touches.popleft()
            for key, at in list(watching.items()):
                if now - at > self.window:
                    watching.pop(key, None)
            until = self._until.get(client, 0.0)
            if until and now >= until:
                self._until.pop(client, None)
                until = 0.0
            repeat = slug in watching
            watching[slug] = now
            if until and not repeat:
                self.blocked += 1
                return False, max(1, int(until - now + 0.999))
            if not repeat:
                touches.append((now, slug))
            distinct = {key for _, key in touches}
            if not until and len(distinct) >= self.limit:
                # 本次放行（第 6 个），之后的新频道才挡——正常换台不会被误伤。
                self._until[client] = now + self.cooldown
            return True, 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "clients": len(self._touches),
                "blocked": self.blocked,
                "cooling": sum(1 for until in self._until.values() if until > time.monotonic()),
            }


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

    __slots__ = ("head", "segs", "seq0", "at", "moved_at", "target", "out", "next_out")

    def __init__(self) -> None:
        self.head: list[str] = []
        self.segs: list[tuple[float, str, str, bool]] = []
        self.seq0 = -1
        self.at = 0.0        # 上次并进清单的时间
        self.moved_at = 0.0  # 上次出现新分片的时间（判断上游有没有冻住）
        # 上游一片有多长（秒）。不同源的差别很大：聚合源常见 3~6 秒，运营商
        # IPTV 官方源是 10 秒。「多久没滚才算冻住」必须跟着它走，写死就会误判。
        self.target = 0.0
        # 下发给播放器的序号（与 segs 一一对应）。**不直接用上游序号**：央视频
        # 换票时会在两个 CDN 域名之间跳，两边的序号基准差 1~2 片（实测回退过 -1）。
        # 上游序号一旦倒退，下发的 MEDIA-SEQUENCE 也跟着倒退，播放器会认定这是
        # 一份「更旧的清单」而丢在一边——ExoPlayer #2872 里这种现象的结论是
        # 「卡在 buffering 且永不恢复」，正是用户报的「卡住不动、不自恢复」。
        # 所以对外序号自己维护，只增不减；内容仍按上游序号对齐。
        self.out: list[int] = []
        self.next_out = 1

    @property
    def frozen_after(self) -> float:
        """多久没出现新分片才算上游真的冻住了（秒）。

        **不能写死。** 原来固定 10 秒，而湖北移动的官方源一片就是 10 秒——
        滚动周期和判定阈值一样长，只要哪次取清单差了半拍（预取间隔相位、网络
        抖动）就会被判成冻住，于是换线路；换过去窗口重来，十秒后又「冻住」，
        再换回来。播放器看到的就是「一直在自动换源、画面一卡一卡」。

        按「两片再加五秒」算：一片 10 秒的源要 25 秒没滚才算真冻住，一片 4 秒
        的源 13 秒，既不误判也不会真的等到天荒地老。
        """
        if self.target <= 0:
            return _FROZEN_AFTER
        return max(_FROZEN_AFTER, self.target * 2.0 + 5.0)

    def feed(self, head, seq, segs, now: float) -> bool:
        """并进一份新清单，返回是否出现了新分片。"""
        self.at = now
        if head:
            self.head = head
        if segs:
            # 用这一份清单里最长的分片当「一片多长」。上游偶尔混进一片 10 秒的
            # 广告，用平均会让阈值偏小、更容易误判，取最长更保守。
            # 放在最前面：下面第一个分支会提前 return，写在那之后永远不生效。
            longest = max((seg[0] for seg in segs if seg[0] > 0), default=0.0)
            if longest > 0:
                self.target = longest

        if not self.segs or seq < 0 or not self.out:
            self.seq0 = max(0, seq)
            self._rebase(segs)
            self.moved_at = now
            return True

        have = self.seq0 + len(self.segs)   # 已经有到（不含）
        if seq > have:
            # 中间缺了好几片，接不上：直接以新清单为准。序号跳变本身就意味着
            # 内容不连续（预告片切回正片、上游序号重置都走这里），给新窗口第一
            # 片补一个断点标记，让播放器重置解码器，而不是拿旧缓冲里的画面回跳。
            self._rebase(segs)
            if self.segs:
                duration, uri, stamp, _disc = self.segs[0]
                self.segs[0] = (duration, uri, stamp, True)
            self.seq0 = seq
            self.moved_at = now
            return True

        # 序号**回退**（``seq < seq0``）不再当成错误：央视频换票会在两个 CDN 域名
        # 之间跳，两边的序号基准差 1~2 片。这时窗口里的内容其实比新清单更靠后，
        # 所以只接收落在窗口内的那片（顺带把地址换成新域名的那份，票据更新鲜），
        # 比窗口还旧的直接忽略——等它追上来。对外序号不受影响，一直往前。
        added = False
        for index, seg in enumerate(segs):
            pos = seq + index - self.seq0
            if pos >= len(self.segs):
                self.segs.append(seg)
                self.out.append(self.next_out)
                self.next_out += 1
                added = True
            elif pos >= 0:
                old = self.segs[pos]
                if seg[1] != old[1]:
                    # 同一片、新地址。运营商 IPTV 的每份清单都带一个新的
                    # ``IASHttpSessionId``，会话过期后旧地址就 403。窗口里留旧
                    # 地址，播放器补缓冲时正好撞上失效的那份，看着像「随机卡住」。
                    self.segs[pos] = (old[0], seg[1], seg[2] or old[2], old[3])
        if added:
            self.moved_at = now
        while len(self.segs) > _WINDOW_SEGMENTS:
            self.segs.pop(0)
            self.out.pop(0)
            self.seq0 += 1
        return added

    def _rebase(self, segs) -> None:
        """整窗重建：内容换成这一份，对外序号从 ``next_out`` 起继续递增。

        **不归零、不回退**。上游换了个 CDN 序号基准时，如果我们跟着把对外序号
        退回去，播放器会认为这份清单比手里的旧，直接丢弃并卡在 buffering。
        """
        self.segs = list(segs)
        base = self.next_out
        self.out = [base + i for i in range(len(self.segs))]
        self.next_out = base + len(self.segs)

    @property
    def first_out(self) -> int:
        """这份清单第一片对外的序号，也就是下发给播放器的 MEDIA-SEQUENCE。"""
        return self.out[0] if self.out else max(0, self.seq0)


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
        # 对外序号取自己维护的那串，不用 seq0：上游换 CDN 时序号会回退 1~2 片，
        # 跟着下发会让播放器认定「这份比手里的旧」而卡在 buffering（见 __init__）。
        out.append(f"#EXT-X-MEDIA-SEQUENCE:{self.first_out}")
        # 告诉播放器「从末尾往前数 3 片开始播」。窗口是 6 片，不写这一行的话
        # VLC / 观屿这类外挂播放器会从窗口最前面开始，等于凭空多落后 15 秒；
        # hls.js 自己有 liveSyncDurationCount，会忽略这行。
        tail = sum(seg[0] for seg in self.segs[-3:])
        if len(self.segs) > 3 and tail > 0:
            out.append(f"#EXT-X-START:TIME-OFFSET={-round(tail, 1)}")
        for duration, uri, stamp, discontinuity in self.segs:
            if discontinuity:
                out.append("#EXT-X-DISCONTINUITY")
            if stamp:
                out.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
            out.append(f"#EXTINF:{duration:.3f},")
            out.append(uri)
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 垫片清单：给 VLC / libVLC 内核的播放器补「缓冲片」
#
# 现象：这类播放器解复用时，读到清单最后一项就停下，不提前去取下一片。于是
# 最新的一整片要等清单里再冒出一片才轮得到它——上游一片 5~9 秒，它手里的内容
# 常常刚好见底，画面停一两秒、自己调大缓冲、卡两三次才顺。上游报的
# TARGETDURATION 越大，它刷清单越慢，断粮越明显。
#
# 对策：在每个分片后面垫一条极短的本机空片，让清单最后一项永远是垫片，最新的
# 整片不再被扣住；TARGETDURATION 报小，逼它按固定节奏来取。垫片是纯 TS 空包，
# 解复用器直接丢弃，不带时间戳，也不改变画面内容。
#
# 只给识别为 libVLC 的请求用；其它播放器下发原清单，互不影响。带加密／分片字节
# 范围／fMP4 初始化段的清单原样下发，不冒险改写（改了反而播不了）。
# ---------------------------------------------------------------------------

# 垫片声明时长（秒）。极小值让它几乎不占播放时间，但必须 > 0 才能是合法的 EXTINF。
_PAD_SECONDS = 0.01
# 开头垫几条：libVLC 冷启动时会跳过清单头两项，不垫就白白少一整片可播。
_PAD_LEADING = 2
# TARGETDURATION 上限：报大会让 libVLC 很久才刷一次清单，长片期间就断粮。
_PAD_TARGET_MAX = 5
# 垫片 TS 包个数。libVLC 一次解复用最多读约 50 个包，垫片必须明显多于这个数，
# 否则一次就读穿、把垫片当成流尾（播完）。空包 188 字节：同步字节 0x47 +
# PID 0x1FFF（空包标志）+ 其余填充。
_PAD_TS_PACKETS = 200
_PAD_TS_BODY = (bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184) * _PAD_TS_PACKETS

# 这些标签出现时清单结构特殊，垫片化会破坏它们，原样下发。
# 注意 DISCONTINUITY **不在**这里：它只是「这一片之前换个时间轴」的标记，
# 原位保留即可，不能因为一个广告断点就放弃整份清单——央视频切回正片必带它。
_PAD_UNSAFE = re.compile(
    r"^#EXT-X-(?:KEY|MAP|BYTERANGE|ENDLIST|PART|STREAM-INF)\b", re.M
)
# libVLC / VLC 的 UA 特征（大小写不敏感）。观屿、Ceau 等外挂播放器基于 libVLC，
# UA 里带 VLC/LibVLC 字样。
_VLC_UA = re.compile(r"(?:^|\s)(?:VLC|LibVLC)/", re.I)


def pad_body() -> bytes:
    """垫片分片的字节内容：一段 TS 空包。返回常量本身（调用方不要改）。"""
    return _PAD_TS_BODY


def is_vlc_ua(user_agent: str) -> bool:
    """这个 UA 是不是 VLC / libVLC 内核的播放器。"""
    return bool(_VLC_UA.search(user_agent or ""))


def padded_playlist(text: str, pad_url: str) -> str | None:
    """给 VLC 内核生成一份「末项永远是垫片」的清单；不改写成则返回 None。

    ``pad_url`` 是本机垫片端点的绝对地址（含密码段）。整片声明时长减去垫片
    时长，保证「整片 + 垫片」相加等于上游真实时长，播放器不会在片边界重算时间线。
    序号翻倍：上游第 n 片排在 2n、它的垫片排在 2n+1，这样同一片在每次刷新里的
    序号稳定，已经播过的项不会被当成新片。

    **三个必须原样带过来的标记**（早期版本把它们丢了，垫片反而造成卡顿）：

    - ``#EXT-X-START``：直播起点。原清单按「离末尾 3 片」给出，丢掉它 VLC 就会
      从窗口最旧的一片开始播，落后几十秒、还容易撞上 CDN 已回收的片。这里保留
      原值，并至少给到 3 个 ``TARGETDURATION``（RFC 8216 要求起点不落在末尾
      3 个 target duration 内）；没有原值时按窗口总时长合成一个。
    - ``#EXT-X-PROGRAM-DATE-TIME``：每片的真实时间。丢了它播放器只能拿窗口
      第一片的时间当直播起点，同样会贴到最旧那头。
    - ``#EXT-X-DISCONTINUITY``：广告切回正片的时间轴断点。它不再是「跳过整份
      清单」的理由，原位保留在对应整片之前，让播放器重置解码器而不是回跳。
    """
    if not pad_url:
        return None
    body = str(text).replace("\r", "")
    if _PAD_UNSAFE.search(body):
        return None

    try:
        first = int(re.search(r"^#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", body, re.M).group(1))
    except (AttributeError, ValueError):
        return None

    start_offset: float | None = None
    match = re.search(r"^#EXT-X-START:TIME-OFFSET=(-?[\d.]+)", body, re.M)
    if match:
        try:
            start_offset = float(match.group(1))
        except ValueError:
            start_offset = None

    segs: list[tuple[float, str, str, bool]] = []
    duration: float | None = None
    stamp = ""
    discontinuity = False
    for line in body.split("\n"):
        value = line.strip()
        if value.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            stamp = value
            continue
        if value.startswith("#EXT-X-DISCONTINUITY"):
            discontinuity = True
            continue
        if value.startswith("#EXTINF:"):
            try:
                duration = float(value[8:].split(",", 1)[0])
            except ValueError:
                duration = None
        elif value and not value.startswith("#"):
            # 只垫 TS 分片；m4s / fMP4 之类不碰
            if not duration or duration <= _PAD_SECONDS or not re.search(r"\.ts(?:$|\?)", value, re.I):
                return None
            segs.append((duration, value, stamp, discontinuity))
            duration = None
            stamp = ""
            discontinuity = False
    if not segs:
        return None

    pad_line = [f"#EXTINF:{_PAD_SECONDS:.3f},", pad_url]
    out = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-MEDIA-SEQUENCE:{first * 2 - _PAD_LEADING}",
        f"#EXT-X-TARGETDURATION:{_PAD_TARGET_MAX}",
    ]
    # 总真实时长（垫片与整片的时长互相抵消，所以就是各整片之和）。
    total = sum(seg[0] for seg in segs)
    floor = float(3 * _PAD_TARGET_MAX)
    if start_offset is None:
        start_offset = -min(floor, total) if total > 0 else None
    elif start_offset > -floor:
        # 原清单给的起点太贴边缘（比如上游一片只有 4 秒、3 片才 12 秒），
        # 垫片把 TARGETDURATION 压到 5 之后就不合规了，往后退到 15 秒。
        start_offset = -floor
    if start_offset is not None:
        out.append(f"#EXT-X-START:TIME-OFFSET={start_offset:.1f}")
    for _ in range(_PAD_LEADING):
        out.extend(pad_line)
    for duration, url, stamp, discontinuity in segs:
        if discontinuity:
            out.append("#EXT-X-DISCONTINUITY")
        if stamp:
            out.append(stamp)
        out.extend([f"#EXTINF:{duration - _PAD_SECONDS:.3f},", url])
        out.extend(pad_line)
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
        auto_switch: bool = True,
        warm: bool = True,
        upstream_denied=None,
        plain_http: bool = True,
    ):
        self._resolver = resolver
        # 上游自己的出口级冷却回调（央视频 8 次拒绝后停 30 秒）。只挡「手里没有
        # 可用地址」的冷启动请求；正在播的台照常用旧地址/旧窗口顶住，不重演
        # 过去整台冷却 30 秒导致画面定住的回归。
        self._upstream_denied = upstream_denied if callable(upstream_denied) else None
        # 解析器支持不支持「换下一路」。老实现只有一个参数，这里探测一次，
        # 免得为了一个可选参数让所有调用方（含测试里的假解析器）都得改。
        try:
            self._resolver_rotates = "rotate" in inspect.signature(resolver).parameters
        except (TypeError, ValueError):
            self._resolver_rotates = False
        self._mode = mode if mode in ("direct", "proxy") else "direct"
        # 央视频分片直连时是否降成 http（见 _PLAIN_SUFFIX）。默认开：不占本机
        # 带宽、不增加本机对上游的请求，只影响「播放器自己怎么连 CDN」。
        self._plain_http = bool(plain_http)
        # 播放中是否允许换到同台的下一条线路。关掉之后只「换票重取同一个地址」，
        # 不再换线路——线路之间来回跳的时候用面板上那个开关先稳住画面。
        self._auto_switch = bool(auto_switch)
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
        # 这张票已经被用了几次。央视频一张票只能用 2 次，用满就主动换，见 _TICKET_REUSE。
        self._ticket_uses: dict[str, int] = {}
        # 最近发生的「异常事件」（环形缓冲，新的在后）。面板和日志都读它。
        # 在这之前服务端一条运行日志都没有：用户说「卡了」，手上只有三个累计
        # 计数，分不清是上游拒、本地退避还是窗口没推进，只能靠猜。
        self._events: deque[tuple[float, str, str]] = deque(maxlen=40)
        self.ticket_rotations = 0   # 主动换票次数（票据用满 2 次就换，不等 403）
        # 最近一次失败的时刻。**只做诊断**，调度看的是下面的 _next_try：
        # 「失败过」不代表「现在不能试」，这行区别是这一版的核心。
        self._failed: dict[str, float] = {}
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
        self._stuck_streak: dict[str, int] = {}               # slug -> 连续几次判定冻住
        self._reticket_at: dict[str, float] = {}              # slug -> 上次为「冻住」换票的时间
        # 每个台上一次请求时刻。用来判断「真的有播放器在看」：两次请求间隔
        # 落在 (_PLAYING_MIN_GAP, _PLAYING_WINDOW] 才算，扫台碰不到这个区间。
        self._hits: dict[str, float] = {}
        # ---- 换台预热 ----
        # slug -> 订阅顺序里的下一个台。由 service 在拼订阅时注入（只有那里知道
        # 真实顺序），relay 只负责「什么时候去取」。
        self._neighbors: dict[str, str] = {}
        self._warm_due: dict[str, tuple[float, str]] = {}   # 目标台 -> (到点时刻, 触发台)
        self._warm_at: dict[str, float] = {}                # slug -> 上次尝试预热的时刻
        self._warm_busy = False
        self.warm_runs = 0                                  # 预热实际发起的次数（诊断用）
        # 这个台下次可以再敲上游的时刻。403 后按 ``_RETRY_DELAYS`` 逐次拉长，
        # 但只算「几秒级」——以前是 30 秒，窗口里的分片刚好被耗干。
        self._next_try: dict[str, float] = {}
        # 这个台连续 403 了几次。成功取到清单就清零，决定下一次退避多久。
        self._retry_hits: dict[str, int] = {}
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
        # 面板上「出口退避」那一格的数字，跟着按台的退避一起动。
        # **只给面板看，不再参与调度**：以前它会让一个台被拒就停掉所有台的
        # 后台刷新，等于别人一个坏台害你看不了 CCTV-1。
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
        entered = False
        with self._lock:
            previous = self._hits.get(slug)
            gap = now - previous if previous is not None else 0.0
            self._hits[slug] = now
            playing = _PLAYING_MIN_GAP < gap <= _PLAYING_WINDOW
            if playing:
                entered = slug not in self._active
                self._active[slug] = now
        # 刚进入「有人在看」才排一次预热。判定「在播」要求同一个台被反复请求，
        # 所以连点换台（每台只碰一次）永远走不到这里——那正是必须避免预热的时候。
        if entered:
            self._plan_warm(slug)
        return playing

    # ---------------- 换台预热 ----------------
    def set_neighbor_map(self, mapping: dict[str, str] | None) -> None:
        """注入「订阅顺序里下一个央视频台」。传空表示关闭预热。

        顺序只有 service 知道（它在拼订阅时按模块分组排序），这里不猜。
        """
        with self._lock:
            self._neighbors = dict(mapping or {})

    def _plan_warm(self, slug: str) -> None:
        """给这个台的「下一个台」排一次预热，延迟 _WARM_DELAY 执行。"""
        with self._lock:
            target = self._neighbors.get(slug)
            if not target or target == slug:
                return
            now = time.time()
            if now - self._warm_at.get(target, 0.0) < _WARM_MIN_GAP:
                return
            due = self._warm_due.get(target)
            if due is not None and now < due[0]:
                return
            self._warm_due[target] = (now + _WARM_DELAY, slug)

    def warm_async(self, slug: str) -> None:
        """把预热丢到独立线程：它可能要等退避、也可能慢，不能拖住刷新循环。

        已经在预热别的台时直接放弃——预热是锦上添花，不值得为它排队。
        """
        with self._lock:
            if self._warm_busy:
                return
            self._warm_busy = True
        threading.Thread(
            target=self._warm_worker, args=(slug,), name="hd-iptv-warm", daemon=True
        ).start()

    def _warm_worker(self, slug: str) -> None:
        try:
            self.warm(slug)
        except Exception:  # noqa: BLE001 - 预热失败只是回到原来的慢，不能影响别的台
            pass
        finally:
            with self._lock:
                self._warm_busy = False

    def warm(self, slug: str) -> bool:
        """替「还没人看」的台先把票据和清单取回来，返回是否真的取了。

        - **不吃在播台的额度**：出口余量低于 ``_WARM_MIN_TOKENS`` 就不做；
        - **失败不留痕**：不记失败计数、不设退避、不写运行日志（走 ``soft``），
          否则一次预热失败会把目标台标成「刚被拒」，用户切过去反而被挡在门外；
        - **不顶掉新鲜缓存**：45 秒内取过就什么都不做。
        """
        now = time.time()
        with self._lock:
            if now - self._warm_at.get(slug, 0.0) < _WARM_MIN_GAP:
                return False
            # 记「尝试」而不是「成功」：失败了也不该在 20 秒内反复试。
            self._warm_at[slug] = now
            window = self._windows.get(slug)
            raw = self._raw.get(slug)
            if window is not None and window.segs and now - window.at <= _WARM_VALID:
                return False
            if raw is not None and now - raw[0] <= _WARM_VALID:
                return False
            if now < self._next_try.get(slug, 0.0):
                return False
            if self._blackout.get(slug, 0.0) > now:
                return False
        if self._token_level() < _WARM_MIN_TOKENS:
            return False
        if not self._take_token(0):
            return False
        try:
            text, base = self._fetch_raw(slug, soft=True)
        except Exception:  # noqa: BLE001 - 预热失败不改变任何状态
            return False
        self._absorb(slug, text, base)
        self.warm_runs += 1
        return True

    def _token_level(self) -> float:
        """还剩几个出口配额（只做预测，不扣）。"""
        with self._bucket_lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._tokens_at) * self._rate)
            self._tokens_at = now
            return self._tokens

    # ---------------- 上游 ----------------
    def _call_resolver(self, slug: str, rotate: bool) -> tuple[str, str]:
        """调解析器，``rotate=True`` 表示「这一路不行，换下一路」。

        解析器是构造时从外面塞进来的，老实现只有一个参数。这里先探测签名，
        免得为了一个可选参数把所有调用方（含测试里的假解析器）都改一遍。

        「自动换源」关掉时这里统一把 ``rotate`` 吞掉：换票（``fresh``）照旧，
        换线路不再发生。收在一处的好处是——上面三条换线入口（上游 403、清单
        冻住、冻结换票）不会漏掉任何一个，不用逐处再判断开关。
        """
        if rotate and not self._auto_switch:
            rotate = False
        if rotate and self._resolver_rotates:
            return self._resolver(slug, rotate=True)  # type: ignore[call-arg]
        return self._resolver(slug)

    def upstream(
        self, slug: str, *, fresh: bool = False, rotate: bool = False, soft: bool = False
    ) -> tuple[str, str]:
        """取该频道当前可用的上游地址与 UA。

        ``fresh=True`` 表示「刚被上游拒绝，想换一个」，受冷却时间约束：
        短时间内重复失败只试一次，避免把接口敲到限流。

        ``rotate=True`` 是更重的一档：不只是换一张票，而是**换一条线路**。
        频道级 slug 下同一个台挂着好几条源，第一路 403 了就轮到第二路——
        这是「列表里只写一行」还不会「一挂就播不了」的关键。

        ``soft=True`` 给预热用：失败不记统计、不设退避。预热只是「提前试试」，
        失败后用户切过去会当场重试，不该被预热留下的退避挡在门外。

        三道闸，任何一道关着就**一枪不打上游**：

        1. 这个台刚被拒（主备全 403）→ 冷却 30 秒；
        2. 这个台正版权停播 → 冷却到点再问；
        3. 换票预算用完 → 本地直接回错，别把出口 IP 打进限流。
        """
        now = time.time()

        # 「自动换源」关掉时，换线在入口就归一成「换票重取同一个地址」。
        # 必须在下面清窗口之前处理：否则线路没换、窗口却按「换了线路」清空了，
        # 播放器手里的清单会突然变短，看起来就像画面断了。
        rotate = bool(rotate) and self._auto_switch

        # 静态源（地址直接编在 slug 里）：没有票要换、没有版权停播要判，也不该
        # 占「换票预算」——直接返回，一个接口请求都不打。放在最前面，是为了让它
        # 完全不碰下面那套给央视频设计的退避 / 预算逻辑。
        static = static_url(slug)
        if static:
            return static, slug_ua(slug)

        # 换线路一定意味着要重新向上游要地址，不能走「手里还有旧地址就照旧给」。
        # 只对支持换线的解析器生效：央视频那类源只有一条路，清了窗口反而会让
        # 播放器手里的清单突然变短。
        if rotate and self._resolver_rotates:
            fresh = True
            # 旧线路攒下的分片窗口必须丢掉：新线路的分片地址完全是另一套，
            # 混在一起发出去，播放器会去取一堆已经不属于这条线路的分片。
            with self._lock:
                self._windows.pop(slug, None)
                self._raw.pop(slug, None)

        with self._lock:
            hit = self._cache.get(slug)
            # 「这个台下次可以再敲」的时刻。注意它只有几秒（见 _RETRY_DELAYS），
            # 不再是 30 秒——那 30 秒恰好是画面被耗干的时间。
            retry_at = self._next_try.get(slug, 0.0)
            blackout_until = self._blackout.get(slug, 0.0)

        if blackout_until > now:
            # 官方说这个台「应版权方要求暂停提供直播信号」。一档节目没版权时整档
            # 都是这句话，播一次问一次没有任何意义，只会一次张票往外打。
            raise RelayError(self._errors.get(slug, "该频道应版权方要求暂停直播"))

        if hit and not fresh:
            # 地址用久了要主动换票。CDN 对同一张票据上的重复请求会越来越容易
            # 回 403（换一个 token 就正常），所以别一直抱着同一张用。
            #
            # 央视频这类源还有更硬的限制：一张票只能用 2 次。只按 TTL 复用会
            # 在第 3 次必撞 403，所以要按「用了几次」主动换，见 _TICKET_REUSE。
            with self._lock:
                uses = self._ticket_uses.get(slug, 0)
                limited = _is_ysp_url(hit[1])
                if now - hit[0] <= self._addr_ttl and (not limited or uses < _TICKET_REUSE):
                    self._ticket_uses[slug] = uses + 1
                    return hit[1], hit[2]
                if limited and uses >= _TICKET_REUSE:
                    self.ticket_rotations += 1
            fresh = True

        if retry_at > now and not fresh:
            # 刚被上游拒过，退避期还没到点：手里有旧地址就继续用（播放器那边
            # 的窗口还在放），没有就把上次的错抛出去。
            if hit:
                return hit[1], hit[2]
            raise RelayError(self._errors.get(slug, "上游暂时不可用，稍后自动重试"))

        # 出口级退避（``_forbidden_until``）**只停后台预取**，不挡这里的同步取：
        # 一个台 403 就把所有台一起关掉，等于别人一个坏台害你看不了 CCTV-1。
        # 真正防「批量扫台把出口敲爆」的是下面那张换票预算，按 IP 记账的口径更准。

        # 上游出口级冷却中（央视频连续拒绝后的 30 秒）就不再为**新频道**敲门。
        # 手里有旧地址的照旧续播——过去把正在看的台一起冻 30 秒，正好耗干窗口
        # 里那几片，是「播十几秒停一下」的回归点，这里明确避开。
        #
        # 只挡「从来没成功拿到过地址的台」（``not hit``）：地址过期要换票的
        # 在播台照样放行。之前写的是 ``not hit or fresh``，等于在播台地址一过
        # TTL 就被冻住，而它手里的缓冲只有几秒——那是「播一分多钟后彻底不动、
        # 换回来也不播」的直接来源。
        if self._upstream_denied and self._upstream_denied() and not hit:
            raise RelayError("央视频上游限流中，约 30 秒后自动恢复")

        if not hit or fresh:
            # 只有真的要向接口要一张新票时才记预算。抱着旧地址续播不计。
            if not self._take_ticket():
                if hit:
                    return hit[1], hit[2]
                raise RelayError("正在批量请求频道，稍后自动恢复")

        try:
            url, ua = self._call_resolver(slug, rotate)
        except Exception as exc:  # noqa: BLE001 - 解析失败要能兜住
            message = str(exc) or type(exc).__name__
            with self._lock:
                # 域名解析失败是**本机**的事，一个字节都没发到上游去：不该按
                # 「上游失败」冷却 20 秒。否则用户点一次黑屏，20 秒内再点还是
                # 同一句错误，看起来像永久坏了。解析闸门自己有 4 秒时限和 5 秒
                # 失败缓存，重试不会失控。
                #
                # soft（预热）失败一律不记账：`failures` 是面板上那个只增不减的
                # 数字，`_next_try` 会真的把目标台挡住几秒——预热是「顺手提前
                # 试一下」，不能让它的失败变成用户换台失败。
                if not soft:
                    if "DNS" not in message and "解析" not in message:
                        self._failed[slug] = now
                        # 取票接口本身失败（不是分发到 CDN 之后 403）：4 秒内别反复敲。
                        # 比原来的 20 秒短得多。**DNS 解析失败不设退避**——那一个字节
                        # 都没发到上游，挡它只会让用户点一次黑屏、再点还是同一句错。
                        self._next_try[slug] = max(
                            self._next_try.get(slug, 0.0), now + min(self._retry_gap, 4.0)
                        )
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
            # 新票已经算用掉一次（马上要拿它去取清单）。
            self._ticket_uses[slug] = 1
            self._failed.pop(slug, None)
            self._errors.pop(slug, None)
        return url, ua

    def forget(self, slug: str) -> None:
        """丢掉地址与清单缓存，下次请求时换新地址。"""
        with self._lock:
            self._cache.pop(slug, None)
            self._ticket_uses.pop(slug, None)
            self._windows.pop(slug, None)
            self._raw.pop(slug, None)
            self._active.pop(slug, None)
            self._stuck_note.pop(slug, None)
            self._stuck_streak.pop(slug, None)
            self._reticket_at.pop(slug, None)
            self._hits.pop(slug, None)
            self._next_try.pop(slug, None)
            self._retry_hits.pop(slug, None)
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
        auto_switch: bool | None = None,
        plain_http: bool | None = None,
    ) -> None:
        """面板上改了参数后立刻生效，不用重启容器。

        预取间隔调小=更实时但更费上游配额；调大=省配额但换台慢半拍。
        切模式要把已缓存的清单清掉，否则播放器还会拿到旧写法。

        ``auto_switch`` 改到「开」时把连续冻住计数清掉：关着的时候可能已经
        攒了好几次冻住，一开启就立刻换线路，看着像开了就乱跳。
        """
        if prefetch_every is not None:
            self._prefetch_every = max(0.5, float(prefetch_every))
            self._fresh_age = max(_FRESH_AGE, self._prefetch_every + 2.0)
        if cache_bytes is not None:
            self._segments.resize(int(cache_bytes))
        if auto_switch is not None and bool(auto_switch) != self._auto_switch:
            self._auto_switch = bool(auto_switch)
            with self._lock:
                self._stuck_streak.clear()
        if plain_http is not None:
            # 只影响「下一次改写清单时写成什么 scheme」，缓存里的原文不用清；
            # 但已改写好的清单（_playlists）是旧 scheme，要丢掉重写。
            if bool(plain_http) != self._plain_http:
                self._plain_http = bool(plain_http)
                with self._lock:
                    self._playlists.clear()
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

        403 只做「几秒级」的退避，不再整台冷却 30 秒。原因见 ``_RETRY_DELAYS``：
        画面能撑住的时间就是窗口里那几片，冷却得比它久，一定会定住。
        """
        now = time.time()
        with self._lock:
            self._failed[slug] = now
            self._errors[slug] = message
            self.failures += 1
            if "403" in message:
                hits = self._retry_hits.get(slug, 0)
                self._retry_hits[slug] = hits + 1
                delay = _RETRY_DELAYS[min(hits, len(_RETRY_DELAYS) - 1)]
                self._next_try[slug] = now + delay
                # 面板上那一格「出口退避」只用来告诉人「现在在等」，跟着按台
                # 的退避走；不再让它去挡别的台的同步取（那只伤及无辜）。
                self._forbidden_until = max(self._forbidden_until, now + delay)
            else:
                self._retry_hits.pop(slug, None)

    def stats(self) -> dict:
        """给面板看的简况。"""
        now = time.time()
        with self._lock:
            cached = {k: round(now - v[0]) for k, v in self._cache.items()}
            playlists = {k: round(now - v.at, 1) for k, v in self._windows.items()}
            playlists.update({k: round(now - v[0], 1) for k, v in self._raw.items()})
            active = sorted(self._active)
            cooling = {k: round(v - now, 1) for k, v in self._next_try.items() if v > now}
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
            # 按台退避（秒）：这个台刚 403，过几秒再敲。数字很小是正常的
            "cooling": cooling,
            # 版权停播冷却（秒）：官方明确回「暂停提供直播信号」
            "blackout": blackout,
            # 这一分钟已经用掉的换票额度（上限 _TICKET_BUDGET）
            "tickets": tickets,
            "ticket_budget": _TICKET_BUDGET,
            # 主动换票次数：票据用满 2 次就换，不等 403。正常播放每分钟 6~12 次
            # （一个台每十秒换一张）；这个数远大于 12 说明有台在反复重取。
            "ticket_rotations": self.ticket_rotations,
            # 换台预热：实际发起的次数，以及当前知道多少个「下一个台」。
            # 预热失败不计入 failures，也不写 recent——它只是提前试试。
            "warm_runs": self.warm_runs,
            "warm_next": len(self._neighbors),
            # 最近 40 条异常事件（新的在前）。面板据此显示「刚才到底发生了什么」，
            # 不用再靠三个累计计数猜。
            "recent": [
                {"ago": round(now - at, 1), "slug": slug, "msg": msg}
                for at, slug, msg in list(self._events)[::-1]
            ],
            # 域名解析闸门的计数。取票全挂时先看这里：timeouts 一直涨就是解析
            # 卡住（不是上游 403），这时改 DNS 或重启服务才有意义。
            "dns": dns_stats(),
        }

    def _fetch(
        self, url: str, ua: str, *, timeout: float | None = None
    ) -> tuple[int, dict, bytes, str]:
        """取一个上游地址，返回 ``(状态码, 响应头, 正文, 跟随跳转后的最终地址)``。

        最终地址是必须的，不是顺手带的：运营商 IPTV 的清单本身在
        ``ztecdn.hb.chinamobile.com:6060``，302 之后才落到真正的边缘
        （``[2409:8087:...]:6610``），而清单里的分片是**相对路径**。拿原始地址
        当基准拼出来的分片地址还挂在原域名上，播放器再跟一次 302 才到 IPv6——
        没有 IPv6 的盒子在这一步就直接失败。基准必须是最终地址。
        """
        # Referer 不能省：官方 CDN 对缺 Referer 的请求更容易回 403
        headers = {"User-Agent": ua or "qqlive", "Referer": _REFERER}
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as resp:
                return resp.status, dict(resp.headers), resp.read(), resp.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read() or b"", exc.geturl() or url
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

        例外：**上游落在 IPv6 上的，direct 模式也要转手**。运营商 IPTV 的直播
        边缘只下发 IPv6 单播地址（实测移动湖北：强制 IPv4 取清单，302 还是往
        ``[2409:8087:...]`` 跳），而电视盒子、老播放器一大半没有 IPv6——把这种
        地址原样写进清单，播放器连不上就报「格式不支持」。本机有这么一路 IPv6
        出口就够了，让分片从本机过一手，播放端有没有 IPv6 都无所谓。
        """
        # 咪咕地址即使 direct 模式也要经本机转发：最终节点清单不带 CORS 头、
        # 且是 http，浏览器 hls.js 直连会被拦（VLC 也常取不到）。让子清单和
        # 分片走 /relay/ 同源下发，播放器只连本机，绕开 CORS 和混合内容。
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        is_migu = host.endswith("miguvideo.com")
        if self._mode == "direct" and not _ipv6_host(url) and not is_migu:
            return self._plain(url, prefix)
        return self.wrap(slug, url, prefix)

    def _plain(self, url: str, prefix: str = "") -> str:
        """直连地址的必要降级：央视频分片改走 http，绕开盒子的 TLS 栈。

        只有同时满足三个条件才降级，任一不满足都原样返回 https：

        * 开关打开（``self._plain_http``，面板可关，CDN 以后关掉 http 时用）；
        * 主机落在 ``_PLAIN_SUFFIX``（目前只有央视频 CDN）；
        * 面板自身是 http——https 面板里往 http 分片发请求会被浏览器按混合内容
          直接拦掉，那种部署必须保留 https。
        """
        if not self._plain_http:
            return url
        if (prefix or "").strip().lower().startswith("https://"):
            return url
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return url
        if parts.scheme != "https":
            return url
        host = (parts.hostname or "").lower()
        if not host.endswith(_PLAIN_SUFFIX):
            return url
        return urllib.parse.urlunsplit(("http", parts.netloc, parts.path, parts.query, parts.fragment))

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
            backing_off = now < self._next_try.get(slug, 0.0)
        self._ensure_ticker()

        # 新鲜的：直接发。
        if window is not None and window.segs and now - window.at <= self._fresh_age:
            return self._rewrite(window.text(), "", slug, prefix), False, window.first_out
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
            return self._rewrite(window.text(), "", slug, prefix), stale, window.first_out
        if raw is not None:
            if now - raw[0] > _STALE_MAX:
                return None
            return self._rewrite(raw[1], raw[2], slug, prefix), stale, raw[3]
        return None

    def last_known(self, slug: str, prefix: str = "") -> tuple[str, bool, int] | None:
        """最后一份清单，**不设年龄上限**。给 HTTP 层兜底用。

        为什么要有这个东西：清单请求返回 5xx 会让 ExoPlayer 抛
        PlaybackException（重试 5 次后停住，App 不重开就永远不动）、让 libVLC
        直接放弃这个流——两者都不会回来问第二次。而返回一份旧清单，播放器顶多
        是「取分片 404、过几秒再来问清单」，只要轮询链不断，上游一恢复就自动接上。

        分片全都过期时画面当然还是不动，但「不动」和「死掉」的区别就在下一次
        清单请求还发不发得出去。
        """
        with self._lock:
            window = self._windows.get(slug)
            raw = self._raw.get(slug)
        if window is not None and window.segs:
            return self._rewrite(window.text(), "", slug, prefix), True, window.first_out
        if raw is not None:
            return self._rewrite(raw[1], raw[2], slug, prefix), True, raw[3]
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
            # 取到了就清零：退避次数只统计「连续」失败，恢复后立刻回到 2 秒起。
            self._retry_hits.pop(slug, None)
            self._next_try.pop(slug, None)
        if not moved:
            self._note_frozen(slug, window, now)
        else:
            # 又开始往前滚了，连续冻住计数清零：下次再冻住要从「换票重取」重新
            # 开始数，不能直接跳到换线路。
            with self._lock:
                self._stuck_streak.pop(slug, None)

    def _note_frozen(self, slug: str, window: "_Window", now: float) -> None:
        """上游迟迟不往前滚时写一条运行日志，30 秒最多一条。"""
        if not window.segs:
            return
        idle = now - window.moved_at
        if idle < window.frozen_after:
            return
        with self._lock:
            last = self._stuck_note.get(slug, 0.0)
            if now - last < 30.0:
                return
            self._stuck_note[slug] = now
        last_seq = window.seq0 + len(window.segs) - 1
        self._event(slug, f"清单停在分片 {last_seq}，{int(idle)} 秒没往前滚", gap=30.0)

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

        # 换台预热：到点、且触发台**还在被看**才真的去取。触发台已经没人看了
        # （换走之后没回来），这次预热就没有对象，白花一次出口配额。
        with self._lock:
            ready = []
            for target, (due_at, source) in list(self._warm_due.items()):
                if due_at > now:
                    continue
                self._warm_due.pop(target, None)
                if now - self._active.get(source, 0.0) <= _ACTIVE_FOR:
                    ready.append(target)
        for target in ready:
            self.warm_async(target)

    def _prefetch(self, slug: str) -> None:
        """替正在观看的播放器去上游要一份新清单。所有调用方都不用等它。"""
        # 退避只挡这个台，而且只有几秒（见 _RETRY_DELAYS）。以前这里用的是
        # 「整条出口退避」，一个台被拒就把所有台的后台刷新一起停掉——那正是
        # 一个人换台失败、全家跟着卡的原因。
        with self._lock:
            backing_off = time.time() < self._next_try.get(slug, 0.0)
        if backing_off:
            return
        if not self._take_token(0):
            self.throttled += 1
            return

        now = time.time()
        with self._lock:
            window = self._windows.get(slug)
            has_window = window is not None and bool(window.segs)
            idle = now - window.moved_at if has_window and window is not None else 0.0
            # 冻住阈值跟着「一片多长」走：运营商官方源常常 10 秒一片，写死 10 秒
            # 等于拿滚动周期当阈值，必然误判成冻住并反复换线路。
            frozen_after = window.frozen_after if has_window and window is not None else _FROZEN_AFTER
            last_reticket = self._reticket_at.get(slug, 0.0)

        # 序号超过「两片 + 5 秒」没滚，基本就是这张票据冻住了，换一张重取。
        fresh = idle >= frozen_after and now - last_reticket >= frozen_after
        rotate = False
        if fresh:
            with self._lock:
                self._reticket_at[slug] = now
                streak = self._stuck_streak.get(slug, 0) + 1
                self._stuck_streak[slug] = streak
            # 第一次冻住只换票重取同一条线路：10 秒一片的源，滚动周期本来就
            # 贴着判定阈值，一次没滚不等于这条路坏了。**连续两次**才换线路。
            # 以前一次就换，换过去窗口又从零开始攒，十秒后再「冻住」再换回来，
            # 播放器看到的就是一直在换源、画面一卡一卡。
            rotate = streak >= 2
        try:
            text, base = self._fetch_raw(slug, fresh=fresh, rotate=rotate)
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

    def _event(self, slug: str, message: str, gap: float = 10.0) -> None:
        """记一条诊断事件：进「最近事件」环形缓冲，同时写到运行日志。

        只放**真的需要人看**的事（取不到清单、窗口冻住、换线路），不要放每次
        取票成功这种噪声——四十条的位置很快就被淹掉了。

        ``gap`` 秒内同一个台的同一条消息只记一次：403 是每十秒一次的正常开销，
        不限流的话四十条缓冲三分钟就被它冲干净，反而看不到真正的问题。
        """
        now = time.time()
        with self._lock:
            if self._events:
                at, last_slug, last_msg = self._events[-1]
                if last_slug == slug and last_msg == message and now - at < gap:
                    return
            self._events.append((now, slug, message))
        self._note(f"[{slug}] {message}")

    def _fetch_raw(
        self, slug: str, *, fresh: bool = False, rotate: bool = False, soft: bool = False
    ) -> tuple[str, str]:
        """去上游取一份**原始**清单，连它的基准地址一起返回。

        为什么不在这里顺手改写：同一个台可能同时被内网和外网访问，改写结果
        依赖对外地址；存原文、发的时候再改写，一份缓存就够所有人用。

        ``soft=True``（预热）：被拒不重试、失败不记账。预热的目的是「省下一次
        换台的等待」，多花 1 秒重试反而比不预热更慢。
        """
        # 这个台刚被拒：退避期内一枪不打上游。``upstream()`` 在有旧地址时会
        # 「照旧返回」，光靠它挡不住——请求照样会打到上游再被拒一次，播放器的
        # 毫秒级重试叠上来就是一片 403。这里必须直接拦在取清单之前。
        with self._lock:
            cooling_channel = time.time() < self._next_try.get(slug, 0.0)
        if cooling_channel and not fresh:
            raise RelayError("本台线路暂时受限，几秒后自动重试")

        url, ua = self.upstream(slug, fresh=fresh, rotate=rotate, soft=soft)
        status, _, body, final = self._fetch(url, ua)
        if status in _GONE and not soft:
            # 同一个清单地址连取几次就会被 CDN 拒（实测 2~5 次后开始 403，
            # 换张新票立刻恢复）。这里先安静 1 秒再换票重取——1 秒是实测能
            # 把「刚被拒」的窗口熬过去的最短时间，再短只会被拒得一样干脆。
            #
            # 这里顺手 rotate：一个台挂着好几条源时，「这条 403 了」最常见的
            # 原因就是这条线路本身不行，换条路比死磕同一个域名划算得多。
            # 只有一条线路时 resolver 会原地返回同一个地址，退化成原来的行为。
            time.sleep(1.0)
            url, ua = self.upstream(slug, fresh=True, rotate=True)
            status, _, body, final = self._fetch(url, ua)
        if status != 200 or not body:
            if not soft:
                self._mark_failed(slug, f"上游返回 {status}")
                # 这一条是「播不出来」的直接证据：面板上看到它密集出现，就是出口
                # 真的在被拒；只有零星几条则属于正常换票开销。
                self._event(slug, f"取清单失败：上游返回 {status}")
            raise RelayError(f"上游返回 {status}，取不到播放清单")

        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text:
            if not soft:
                self._mark_failed(slug, "上游返回的不是 m3u8 清单")
            raise RelayError("上游返回的不是 m3u8 清单")
        # 基准用**跳转之后**的地址：清单里的分片多是相对路径，拿原始地址拼会把
        # 播放器又指回那个还在下发 302 的域名（运营商 IPTV 就是这样）。
        return text, final or url

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

        status, headers, body, final = self._fetch(url, ua, timeout=self._seg_timeout)
        if status in _GONE:
            # 上游偶发拒绝很常见，稍等一下再试一次，通常就过了
            time.sleep(_RETRY_PAUSE)
            status, headers, body, final = self._fetch(url, ua, timeout=self._seg_timeout)
        if status in _GONE:
            self._mark_failed(slug, f"分片被拒（{status}）")
            raise RelayError(f"上游返回 {status}")
        if status != 200:
            raise RelayError(f"上游返回 {status}")

        # 子清单也是 m3u8，里面的分片地址同样要换成走本机，否则播放器会
        # 拿着上游地址直连，又回到被 UA 拒绝的老问题
        if _is_playlist(url, headers, body):
            text = body.decode("utf-8", "replace")
            rewritten = self._rewrite(text, final or url, slug, prefix).encode("utf-8")
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


__all__ = ["Relay", "RelayError", "ScanGuard", "allowed", "encode", "decode", "parse_range"]
