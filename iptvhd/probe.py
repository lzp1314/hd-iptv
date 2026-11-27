"""并发探测 + 测速。

一条流要过四关才算「高清可用」：
  1. HTTP 能连上（状态码 200/206）
  2. 内容确实是直播（m3u8 播放列表，或者 video/* 直连流）
  3. 能读到真实媒体分片（播放列表指向的第一个 ts/fmp4 分片可取）
  4. 分辨率与速率达标
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable

from . import hls, videoinfo, ysp
from .models import Stream
from .net import HttpClient

_OK_STATUS = (200, 206)

# 分片数超过这个数，基本可以断定不是直播：直播 HLS 只保留几片到十几片的滑动窗口。
# 实测见过 2096 片的清单（快手 CDN 上的视频文件），速度漂亮但播完就黑屏。
_VOD_SEGMENTS = 60

# 分片数不超过这个值，说明上游只留了个很短的滑动窗口，必须额外确认它在滚动。
# 这是「点进去播十几秒就停」和「能一直看」的分水岭。定在 8 而不是 6：窗口 7~8
# 片的源照样会在几十秒内放完，而它们之前完全跳过了复核——正是「播一会儿就停」
# 反馈里最常见的那一类。整个批次共用一次等待（见 _verify_roll），放宽只多几条
# 清单请求，不多花时间。
_SHORT_WINDOW = 8
# 复核清单是否在滚之前统一等多久。要大于上游分片时长，否则「正常但片长 6 秒」
# 的源会被误判冻结（湖南卫视的官方 2160P 源就是这么被冤枉的：33 Mbps、分片
# 全 200，只是片长 6 秒）。所有待复核的源共用这一次等待，不是每条各睡一遍。
_ROLL_GAP = 8.0

_SEQ_LINE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)")


def _seq_of(text: str) -> int:
    """清单滚到第几片；没有这个标签返回 -1（主清单常见）。"""
    match = _SEQ_LINE.search(text or "")
    return int(match.group(1)) if match else -1

# 采集阶段的验证结论能用多久。部分平台的清单 5 秒滚一轮、只留 3 片，
# 超过这个时间旧结论就不再可靠，得重新读清单。
_REUSE_WINDOW = 120.0

# 全局节拍器：对「热门接口」类源限速，避免并发探测触发对方的
# 请求频率限制——被限流的表现是一律 403，和死链长得一模一样，很容易误杀。
_pace_lock = threading.Lock()
_pace_last = 0.0


def _pace(min_gap: float = 0.25) -> None:
    """串行化发出请求，保证相邻两次至少间隔 min_gap 秒。"""
    global _pace_last
    with _pace_lock:
        now = time.monotonic()
        wait = _pace_last + min_gap - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _pace_last = now


# 单条流探测的默认时限（秒）：连接 + 读清单 + 读分片加起来。
# 4 Mbps 的 1080P 拉 256KB 分片约 0.5 秒，15 秒足够；超过这个数还没完的，
# 基本都是「连得上但一个字节一个字节往回滴」的废源，等下去只会让整轮更新卡死。
DEFAULT_PER_STREAM_TIMEOUT = 15.0

# 整批探测的总预算（秒）。到点就用已经回来的结果收工，不再等长尾。
# 跟着 sample_bytes 一起放宽：采样从 256KB 提到 512KB，每条流多读一倍数据，
# 预算还卡在 180 秒的话会有更多台排在后面根本没轮到——加严反而变成少台。
DEFAULT_BATCH_BUDGET = 210.0

# 多久没有一条探测完成就提醒一次（面板上能看到，不用等半小时才发现）
STALL_WARN_SECONDS = 15.0

# 截止时间过了这么久还没回来，调度线程就不再等它：线程可能卡在 socket 之外
# 的调用上（DNS 解析没有超时可设），那种卡法只能由调度侧放手。
DEADLINE_GRACE = 5.0


class _Slow(Exception):
    """单条流超过截止时间还在滴流：判定为慢源，立刻放手。"""

    def __init__(self, reason: str = "读取超时（慢源）"):
        super().__init__(reason)
        self.reason = reason


def _fetch(client: HttpClient, url: str, *, limit: int = 0, headers: dict | None = None,
           deadline: float | None = None):
    """带截止时间的取数：被截断就等于这条源不值得等。"""
    resp = client.get(url, limit=limit, headers=headers, deadline=deadline)
    # 测试替身/旧客户端可能没有这个字段，按「没截断」处理
    if getattr(resp, "truncated", False):
        detail = getattr(resp, "error", "") or ""
        # net 层已经给出具体原因（DNS 超时等），别一律推给「慢源」
        raise _Slow("读取超时（慢源）" if detail in ("", "读取超时") else detail)
    return resp


def _probe_prefetched(stream: Stream, client: HttpClient, sample_bytes: int,
                      deadline: float | None = None) -> Stream:
    """采集阶段已经验证过清单和首个分片的流。

    这类源（部分平台）的清单只有 3 个分片、5 秒滚一轮，采集时拿到的分片地址过一分钟
    就被 CDN 回收了。以前这里拿那个旧地址重试，403/404 一律判死——那正是「明明
    采集到 55 个台，面板只剩 36 个」的来源。所以只认采集时的验证结果，不再重复
    打上游：既省掉每条流一次请求，也不会拿过期地址冤枉一条能播的流。
    """
    if stream.verified_at and time.time() - stream.verified_at <= _REUSE_WINDOW:
        stream.ok = True
        stream.status = stream.status or 200
        if not stream.height:
            stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name)
        return stream

    # 采集距今太久，当时的结论已经不作数：重新读一份清单，用里面最新的分片测速。
    extra = {"User-Agent": stream.user_agent} if stream.user_agent else None
    ysp.pace()
    fresh = _fetch(client, stream.url, limit=131072, headers=extra, deadline=deadline)
    text = fresh.text if fresh.body[:64].lstrip().startswith(b"#EXTM3U") else ""
    seg_url = ""
    if text:
        info = hls.parse_media(text, fresh.url or stream.url)
        seg_url = info["first_segment"]
        if not stream.segment_duration:
            stream.segment_duration = info["segment_duration"] or info["target_duration"]
    seg = (
        _fetch(client, seg_url, limit=sample_bytes, headers=extra, deadline=deadline)
        if seg_url
        else fresh
    )

    stream.status = seg.status
    if seg.status in _OK_STATUS and seg.body:
        stream.ok = True
        stream.speed_mbps = seg.speed_mbps
        stream.delay_ms = max(stream.delay_ms, seg.ttfb_ms)
        _read_resolution(stream, seg.body)
        _estimate_bitrate(stream, seg, sample_bytes)
    elif seg.status in (403, 429):
        # 限流：播放列表在采集阶段已确认可用，播放器按正常节奏取片不会触发
        stream.ok = True
        stream.note = f"分片限流({seg.status})，未测速"
    else:
        stream.ok = False
        stream.error = f"分片不可用: {seg.error or seg.status}"
    if not stream.height:
        stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name)
    return stream


def probe_one(
    stream: Stream,
    client: HttpClient,
    sample_bytes: int = 262144,
    deadline: float | None = None,
) -> Stream:
    """探测单条流，结果写回 stream 自身。

    部分平台（如综合频道）要求特定 UA 才放行分片，所以流自带 UA 时优先用它。
    ``deadline`` 为 ``time.monotonic()`` 口径的绝对截止时刻，到点即判为慢源。
    """
    try:
        _probe_one(stream, client, sample_bytes, deadline)
    except _Slow as exc:
        stream.ok = False
        stream.speed_mbps = 0.0
        stream.error = exc.reason
        stream.note = "长时间读不完，已丢弃"
    # 收尾时已经过点的，一律不算可用：哪怕它最后挤完了，速率也撑不起播放
    if deadline is not None and time.monotonic() > deadline and not stream.error:
        stream.ok = False
        stream.error = "探测超时"
    return stream


def _probe_one(
    stream: Stream,
    client: HttpClient,
    sample_bytes: int = 262144,
    deadline: float | None = None,
) -> Stream:
    if stream.playlist_only and not stream.url:
        # 采集阶段只登记了频道、没取地址：能不能播由播放时的中转层决定，
        # 这里不该为它向上游发请求（那正是批量探测被限流的来源）
        stream.ok = True
        if not stream.height:
            stream.height = stream.hint_height
        return stream

    if stream.playlist_only and stream.first_segment:
        return _probe_prefetched(stream, client, sample_bytes, deadline)

    extra = {"User-Agent": stream.user_agent} if stream.user_agent else None
    if stream.playlist_only:
        _pace()
    resp = _fetch(client, stream.url, limit=sample_bytes, headers=extra, deadline=deadline)
    if stream.playlist_only and resp.status in (403, 429):
        # 这类源对突发请求敏感，退避后再试几次，别把限流误判成死链
        for _ in range(3):
            time.sleep(2.0)
            _pace()
            resp = _fetch(client, stream.url, limit=sample_bytes, headers=extra, deadline=deadline)
            if resp.status not in (403, 429):
                break
    stream.status = resp.status
    stream.delay_ms = resp.ttfb_ms
    # 跟随跳转后的最终地址：几百条源里，广告农场和「一址多台」的占位源只有
    # 比对最终落点才抓得出来（原始地址各不相同，按 URL 去重完全看不见）。
    if resp.url:
        stream.final_url = resp.url

    if resp.status not in _OK_STATUS:
        stream.ok = False
        stream.error = resp.error or f"HTTP {resp.status}"
        return stream

    body_text = ""
    if resp.body[:64].lstrip().startswith(b"#EXTM3U"):
        body_text = resp.text

    if body_text:
        _probe_hls(stream, client, body_text, resp, sample_bytes, deadline)
        return stream

    # 非播放列表：直连 ts/flv/mp4，能下到字节即可认为可用
    stream.ok = bool(resp.body)
    stream.speed_mbps = resp.speed_mbps
    # 本地自定义源（rtp2http / udpxy）多数是裸 TS，里面同样带 SPS。
    # 以前只有 HLS 分片会读码流，裸流的 height 永远是 0，面板只能显示「未知」，
    # 那些台在有画质门槛的视图里也跟着没标签。这里复用同一套解析，读不到再兜底。
    _read_resolution(stream, resp.body)
    if not stream.height:
        stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name)
    if not stream.ok:
        stream.error = "响应体为空"
    return stream


def _probe_hls(stream: Stream, client: HttpClient, body_text: str, resp, sample_bytes: int,
               deadline: float | None = None) -> None:
    base = resp.url or stream.url
    text = body_text
    variant = None

    # 主播放列表 → 取最高码率变体，继续往下探
    if hls.is_master(text):
        variants = hls.parse_variants(text, base)
        variant = hls.best_variant(variants)
        if variant is None:
            stream.error = "主播放列表无可用变体"
            return
        stream.width = variant.width
        stream.height = variant.height
        stream.bandwidth = variant.bandwidth
        stream.codec = variant.codecs
        sub = _fetch(client, variant.uri, limit=65536, deadline=deadline)
        if sub.body[:64].lstrip().startswith(b"#EXTM3U"):
            text = sub.text
            base = sub.url or variant.uri
        else:
            # 主清单能打开、子清单却取不到，说明这只是一个空壳地址（点播台常见），
            # 播放器切进去照样黑屏，不能算可用。
            stream.ok = False
            stream.error = f"变体清单不可用: {sub.error or sub.status}"
            return

    info = hls.parse_media(text, base)
    if info["segments"] <= 0:
        stream.error = "播放列表无分片"
        return

    # 点播清单要挡掉：带 #EXT-X-ENDLIST 的播完就停；分片数远超直播窗口的
    # （实测见过 2096 片的）本质是视频文件，播完露出结局就黑屏。
    if info["has_endlist"] or info["segments"] > _VOD_SEGMENTS:
        stream.ok = False
        stream.error = f"点播清单（{info['segments']} 片），非直播"
        return

    if not stream.segment_duration:
        stream.segment_duration = info["segment_duration"] or info["target_duration"]

    # 读「最后一片」而不是第一片：第一片是 15 秒前的内容，上游早就不管它了，
    # 拿它测速永远漂亮。真正会断的是清单不滚、最新片 404 的那种源——
    # 107.150.60.122 的湖南卫视就是这样：清单 3 片，末片 404，却因为读了
    # 首片而被判「可用」，排到播放列表第一位，播 15 秒就停。
    seg_url = info["last_segment"] or info["first_segment"]
    if seg_url and stream.playlist_only:
        _pace()
    seg = _fetch(client, seg_url, limit=sample_bytes, deadline=deadline) if seg_url else None
    if seg is not None and (seg.status not in _OK_STATUS or not seg.body):
        # 分不清「限流」和「死链」是之前最大的坑：只要 playlist_only 为真，
        # 404 / 连不上 / 5xx 一律按「未测速」放行，死源就这么混进了播放列表。
        # 限流的特征是 403 / 429，只有这两种宽容；其余一律判死。
        soft = seg.status in (403, 429)
        if soft:
            for _ in range(2):
                time.sleep(1.5)
                _pace()
                again = _fetch(client, seg_url, limit=sample_bytes, deadline=deadline)
                if again.status in _OK_STATUS and again.body:
                    seg = again
                    break
        if seg.status not in _OK_STATUS or not seg.body:
            if soft and stream.playlist_only:
                stream.note = f"分片限流({seg.status})，未测速"
                if not stream.height:
                    stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name)
                stream.ok = True
                return
            stream.ok = False
            stream.error = f"分片不可用: {seg.error or seg.status}"
            return

    # 分片读到了：测速、读真实分辨率、估码率。
    # 这三件事曾经被写在「分片不可用」的分支里，等于只在失败时统计——成功
    # 的流永远是 0 速率、0 分辨率，随后被「画质 ≥720P」整批筛掉。卫视的
    # 直连源几乎全军覆没、只剩央视频兜底，根子就在这里。
    if seg is not None and seg.body and seg.status in _OK_STATUS:
        stream.speed_mbps = seg.speed_mbps
        if seg.ttfb_ms:
            stream.delay_ms = max(stream.delay_ms, seg.ttfb_ms)
        _read_resolution(stream, seg.body)
        _estimate_bitrate(stream, seg, sample_bytes)

    if not stream.height:
        # 媒体清单本身不带分辨率（单码率直播常见），优先用采集端已知的档位
        stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name, stream.raw_name)

    # 短窗口清单还要确认它真的在往前滚。清单不滚的源，播放器播完手里这几片
    # 就停在原地。这里只登记「待复核」，真正的等待与复核放到 probe_many 里
    # 统一做——每条源各睡 4 秒的话，332 条要多花好几分钟，而它们完全可以
    # 一起等同一个 8 秒。
    #
    # 以前这里写着 `not stream.playlist_only`，等于把订阅类源整批排除在复核
    # 之外——而它们恰恰是短窗口的重灾区：3 片的列表播十几秒就停，正是用户说的
    # 「点进去能看一会儿，然后卡住」。复核是统一等 8 秒，不随条数线性增长，
    # 放开后不增加总耗时。
    if info["segments"] <= _SHORT_WINDOW:
        seq = _seq_of(text)
        if seq >= 0:
            stream.roll_url = base
            stream.roll_seq = seq

    stream.ok = True


def _estimate_bitrate(stream: Stream, resp, sample_limit: int = 0) -> None:
    """用「分片大小 ÷ 分片时长」估算真实码率。

    这个数字比下载测速更有意义：测速反映 NAS 能不能拉得动，码率反映这条流
    本身有多重。1080P 直播通常在 4~8 Mbps，标清 1~2 Mbps。
    服务端没给 Content-Length（分块传输）时跳过，不瞎猜。
    """
    if stream.bandwidth:
        return
    headers = resp.headers or {}
    size = 0
    # 206 的 Content-Range 写着真分片总长度（bytes 0-262143/7834900），
    # 这是最准的一个数，优先用它
    rng = str(headers.get("content-range") or "")
    if "/" in rng:
        total = rng.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            size = int(total)
    if not size:
        try:
            size = int(headers.get("content-length") or 0)
        except (TypeError, ValueError):
            return
    if size <= 0 or stream.segment_duration <= 0:
        return
    # 长度恰好等于我们要的采样量，说明上游认了 Range，给的只是那一段：
    # 拿它算出来的码率是假的（实测 256KB÷10 秒 = 210kbps，而真值是 7.8Mbps）。
    # 这种响应上面已经从 Content-Range 取过真长度了，取不到就宁可不显示。
    if sample_limit and size == sample_limit:
        return
    stream.bandwidth = int(size * 8 / stream.segment_duration)


def _read_resolution(stream: Stream, data: bytes) -> None:
    """从分片码流里读真实分辨率。

    列表/接口上标的档位经常名不副实（720p 上变换冒充 1080P），只有 SPS 里的
    宽高是准的。读不到就保留采集端的估计值，不因此判死。
    """
    found = videoinfo.probe(data)
    if not found.ok:
        return
    stream.width = found.width
    stream.height = found.height
    if found.codec:
        stream.codec = found.codec
    if stream.hint_height and found.height < stream.hint_height:
        stream.note = f"实际 {found.height}P（接口标 {stream.hint_height}P）"


def apply_sample(stream: Stream, meta: dict) -> None:
    """把采集阶段顺手读到的那一片数据用上。

    解析器（综合频道）在换地址时已经下载过一片，这里直接从中读出真实分辨率和
    码率，探测阶段就不用再向同一个 CDN 打第二次。
    """
    body = meta.get("segment_body") or b""
    if not body:
        return
    stream.speed_mbps = float(meta.get("speed_mbps") or 0.0)
    stream.delay_ms = int(meta.get("ttfb_ms") or 0)
    stream.status = int(meta.get("segment_status") or 0)
    _read_resolution(stream, body)
    if stream.bandwidth or stream.segment_duration <= 0:
        return
    stream.bandwidth = int(len(body) * 8 / stream.segment_duration)


def _probe_with_budget(
    stream: Stream,
    client: HttpClient,
    sample_bytes: int,
    timeout: float,
    holder: list,
) -> Stream:
    """在工作线程里才算截止时刻。

    关键是「从真正开始探测时算」，不能用提交任务时算：332 条排 14 批，
    后面批次的流光排队就超过时限了，那样会把本来就是好的源全部误杀。

    ``holder`` 把截止时刻回传给调度线程——线程万一卡在非 socket 的调用上
    （典型是 DNS 解析），由调度线程按这个时间主动放手，不再等它。
    """
    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    holder.append(deadline)
    return probe_one(stream, client, sample_bytes, deadline)


def _verify_roll(streams: list[Stream], client: HttpClient, concurrency: int) -> None:
    """统一复核「短窗口清单是否在往前滚」。

    直播 HLS 只保留几片，播放器把这几个放完就只能等新片。上游要是压根不滚，
    画面就停在原地——公开转发源里这种空壳非常多：清单能拉到、分片也能下，
    序号却一动不动，播十几秒必然卡死。

    做法是整个批次共用**一次**等待：所有待复核的源一起睡 8 秒，再并发各取
    一次清单比对序号。逐条各睡一遍的话，三百多条要多花好几分钟。
    """
    targets = [s for s in streams if s.ok and s.roll_url and s.roll_seq >= 0]
    if not targets:
        return

    time.sleep(_ROLL_GAP)

    def check(stream: Stream) -> None:
        try:
            resp = client.get(stream.roll_url, limit=131072)
        except Exception:  # noqa: BLE001 - 复核失败按「不动」处理会误杀，直接放过
            return
        if not resp.body[:64].lstrip().startswith(b"#EXTM3U"):
            return
        seq_now = _seq_of(resp.text)
        if seq_now >= 0 and seq_now == stream.roll_seq:
            stream.ok = False
            stream.error = "清单不滚动（冻结），播十几秒就停"

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(check, targets))


def probe_many(
    streams: Iterable[Stream],
    client: HttpClient,
    *,
    concurrency: int = 24,
    sample_bytes: int = 262144,
    per_stream_timeout: float = DEFAULT_PER_STREAM_TIMEOUT,
    budget: float = DEFAULT_BATCH_BUDGET,
    deadline_grace: float = DEADLINE_GRACE,
    on_progress: Callable[[int, int], None] | None = None,
    on_stall: Callable[[int, float], None] | None = None,
) -> list[Stream]:
    """并发探测，保持输入顺序返回。

    两道闸门，缺一不可：

    * ``per_stream_timeout``：单条流的绝对时限，慢滴流到点即弃；
    * ``budget``：整批的总预算，到点就用已回来的结果收工。

    以前只靠 socket 超时（每 8 秒无数据才断），碰上「连得上、但一个字节一个
    字节往回挤」的源就会无限期挂着，一轮更新能卡半小时——面板上表现为
    ``[探测] 297/332`` 再也不动。
    """
    items = list(streams)
    total = len(items)
    if not total:
        return []

    done = 0
    per_stream = per_stream_timeout if per_stream_timeout and per_stream_timeout > 0 else 0.0
    deadline_stop = time.monotonic() + budget if budget and budget > 0 else None

    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    try:
        futures = {}
        holders: dict = {}
        for s in items:
            holder: list = []
            fut = pool.submit(_probe_with_budget, s, client, sample_bytes, per_stream, holder)
            futures[fut] = s
            holders[fut] = holder

        pending = set(futures)
        last_done = time.monotonic()
        last_warn = 0.0
        while pending:
            now = time.monotonic()
            wait_for = STALL_WARN_SECONDS
            if deadline_stop is not None:
                left = deadline_stop - now
                if left <= 0:
                    break
                wait_for = min(wait_for, left)
            # 睡太久就赶不上「到点放手」这一步：等到最近一条的截止时刻就醒
            for fut in pending:
                holder = holders[fut]
                if holder and holder[0] is not None:
                    wait_for = min(wait_for, max(0.02, holder[0] + deadline_grace - now))
            finished, pending = wait(pending, timeout=wait_for, return_when=FIRST_COMPLETED)
            for fut in finished:
                stream = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - 单条流异常不能中断整批
                    stream.ok = False
                    stream.error = f"探测异常: {type(exc).__name__}: {exc}"
                done += 1
                if on_progress:
                    on_progress(done, total)
            if finished:
                last_done = time.monotonic()

            # 超时已久的直接放手：线程可能卡在 socket 之外的地方（DNS 解析），
            # 那种调用没有超时可设，只能调度线程这边不再等它。
            now = time.monotonic()
            for fut in list(pending):
                deadline = holders[fut][0] if holders[fut] else None
                if deadline is not None and now > deadline + deadline_grace:
                    stream = futures[fut]
                    fut.cancel()
                    stream.ok = False
                    stream.error = stream.error or "探测超时"
                    pending.discard(fut)
                    done += 1
                    if on_progress:
                        on_progress(done, total)

            if not finished and on_stall:
                stalled = time.monotonic() - last_done
                if stalled >= STALL_WARN_SECONDS and time.monotonic() - last_warn >= STALL_WARN_SECONDS:
                    on_stall(len(pending), stalled)
                    last_warn = time.monotonic()

        # 到点还没回来的：排队的直接取消，正在跑的交给各自的单条时限收尾，
        # 这里先按「超时」记下，别让它们拖着整轮不结束。
        for fut in pending:
            fut.cancel()
            stream = futures[fut]
            stream.ok = False
            stream.error = stream.error or "探测超时"
    finally:
        # 不能 wait=True：那样等于把「不等长尾」的初衷又撤销了
        pool.shutdown(wait=False, cancel_futures=True)

    _verify_roll(items, client, concurrency)
    return items


# 超时类失败的特征。连不上、来不及、读到一半断了——都算「这条路不通」的信号；
# 403/404 是「这个台没了」，不是「整条网不通」，不能一视同仁地拿来熔断。
_TIMEOUT_HINTS = ("超时", "Timeout", "timed out", "长时间读不完")


def _looks_timeout(stream: Stream) -> bool:
    text = f"{stream.error or ''} {stream.note or ''}"
    return any(hint in text for hint in _TIMEOUT_HINTS)


def _host_of(url: str) -> str:
    """地址的主机名，小写；取不出来返回空串。"""
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _inherit(backup: Stream, winner: Stream) -> None:
    """同台的备用线路：不单独探测，直接继承已探通那条的结论。

    只对**同一主机**的地址允许继承（调用方保证）：同一个台的多条地址常常落在
    同一张 CDN、同一个账号下，探一条就等于探了整组；每条都探既慢又白敲上游
    （143 条串行要半小时，用户看到的就是「卡住不动」）。

    为什么必须限定同主机：一份「本省源」里经常混进别省的入口（实测湖北清单里
    出现过上海的 ``iptvrr.sh.chinamobile.com``），那种地址跨省根本连不通。以前
    按台名分组就继承，等于给它盖了个「已验证 322Mbps」的章，排序时还排在能用
    的线路前面——播放时就卡在它身上等超时。
    """
    backup.ok = True
    backup.status = winner.status or 200
    backup.height = winner.height
    backup.width = winner.width
    backup.bandwidth = winner.bandwidth
    backup.speed_mbps = winner.speed_mbps
    backup.delay_ms = winner.delay_ms
    backup.codec = winner.codec
    backup.note = (backup.note + " " if backup.note else "") + "同台备用线路，未单独探测"


def probe_gentle(
    streams: Iterable[Stream],
    client: HttpClient,
    *,
    sample_bytes: int = 262144,
    per_stream_timeout: float = 8.0,
    gap: float = 1.2,
    abort_ratio: float = 0.6,
    min_tries: int = 8,
    tries_per_channel: int = 2,
    timeout_streak: int = 5,
    on_progress: Callable[[int, int], None] | None = None,
    on_line: Callable[[Stream, float | None, int, int], None] | None = None,
) -> str:
    """串行、留间隔地探测运营商官方源，返回提前收手的原因（空串 = 正常跑完）。

    这类源（移动/电信 IPTV 的 ``1700xxxxxx`` 账号）按出口 IP 记账：一口气并发
    几十条清单请求，CDN 不会回 403，而是把本机打进「302 无限循环」——之后连单
    条也取不到，只能等限频自然过期。所以这里一条一条来，中间留 ``gap`` 秒。

    三道省时的闸，都是被「143 条串行半小时、面板看着像卡死」逼出来的：

    1. **同台只探前 ``tries_per_channel`` 条**。一个台常有 2~4 条地址（内置表 +
       本省自动源各给一条），探通一条就够判断这组可用性，其余继承结论当备用
       线路。143 条因此通常只探 90 来次。
    2. **连续 ``timeout_streak`` 条超时就熔断**。整条网不通时，一条条等满时限
       只是把 30 分钟熬完，一个字都换不回来。403/404 不算超时，不会误触发。
    3. **失败率超过 ``abort_ratio``**（且已试够 ``min_tries`` 条）也收手：那基本
       上就是整个账号被限了，继续打只会让限频窗口拖得更长。

    ``on_line(stream, 用时, 已处理, 总数)`` 每条都回调一次（继承的条目用时为
    ``None``），日志里就能逐条看到「哪个台、多久、什么结果」，不用干等一个百分比。

    也不做「清单是否在滚」的二次复核：那要为每条再等一轮分片时长，等于翻倍敲
    上游；真冻住了，中转层自己会发现并换下一路。
    """
    items = list(streams)
    total = len(items)
    if not total:
        return ""

    # 按台分组但保持原始顺序：内置表的台先探，自动源补进来的同台条目跟在后面。
    groups: list[list[Stream]] = []
    index: dict[str, list[Stream]] = {}
    for stream in items:
        key = stream.channel or stream.raw_name or stream.url
        bucket = index.get(key)
        if bucket is None:
            bucket = []
            index[key] = bucket
            groups.append(bucket)
        bucket.append(stream)

    tries = max(1, tries_per_channel)
    tried = failed = done = 0
    streak = 0
    reason = ""

    def skip_rest(reason_text: str, from_items: list[Stream]) -> None:
        for rest in from_items:
            rest.ok = False
            rest.error = rest.error or reason_text

    for gidx, bucket in enumerate(groups):
        # 主机 -> 该主机的代表结论（None 表示这个主机的第一条就不通）
        hosts: dict[str, Stream | None] = {}
        host_done = 0        # 已处理条数（含继承），用来卡「每个台探几组」
        for sidx, stream in enumerate(bucket):
            host = _host_of(stream.url)
            if host and host in hosts:
                # 同一个主机已经探过：直接抄结论，不再敲上游
                rep = hosts[host]
                if rep is not None:
                    _inherit(stream, rep)
                else:
                    stream.ok = False
                    stream.error = stream.error or "同主机地址已确认不可用"
                host_done += 1
                done += 1
                if on_line:
                    on_line(stream, None, done, total)
                continue

            if host_done >= tries:
                # 探的额度用完了。这里**不能**当备用继承：没真探过就没资格说
                # 它可用，宁可标成未探明（会被过滤掉），也别让它排到能用的
                # 线路前面，让播放器在它身上等超时。
                stream.ok = False
                stream.error = stream.error or "同台已探够线路，本条未单独探测"
                host_done += 1
                done += 1
                if on_line:
                    on_line(stream, None, done, total)
                continue

            deadline = time.monotonic() + per_stream_timeout if per_stream_timeout > 0 else None
            started = time.monotonic()
            try:
                probe_one(stream, client, sample_bytes, deadline)
            except Exception as exc:  # 单条探测出意外，不该把整批带下水
                stream.ok = False
                stream.error = str(exc) or "探测失败"
            elapsed = time.monotonic() - started
            host_done += 1
            tried += 1
            done += 1
            if host and host not in hosts:
                hosts[host] = stream if stream.ok else None
            if on_line:
                on_line(stream, elapsed, done, total)

            if stream.ok:
                streak = 0
            else:
                failed += 1
                streak = streak + 1 if _looks_timeout(stream) else 0
                if streak >= timeout_streak:
                    reason = f"连续 {streak} 条超时，判定这条网当前不通，本轮跳过剩余官方源"
                    break
                if tried >= min_tries and failed / tried > abort_ratio:
                    reason = "失败率过半，判定账号已被限流，本轮跳过剩余官方源"
                    break
            if host_done < min(tries, len(bucket)):
                time.sleep(gap)

        if reason:
            # 熔断：本条剩下的、以及后面所有台，一律跳过，别让整轮卡在等超时上
            rest = list(bucket[sidx + 1:])
            for later in groups[gidx + 1:]:
                rest = rest + later
            skip_rest(reason, rest)
            done = total
            if on_progress:
                on_progress(done, total)
            return reason

        if on_progress:
            on_progress(done, total)
        if done < total and host_done:
            time.sleep(gap)
    return ""
