"""并发探测 + 测速。

一条流要过四关才算「高清可用」：
  1. HTTP 能连上（状态码 200/206）
  2. 内容确实是直播（m3u8 播放列表，或者 video/* 直连流）
  3. 能读到真实媒体分片（播放列表指向的第一个 ts/fmp4 分片可取）
  4. 分辨率与速率达标
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable

from . import hls, videoinfo, ysp
from .models import Stream
from .net import HttpClient

_OK_STATUS = (200, 206)

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
DEFAULT_BATCH_BUDGET = 180.0

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
        _estimate_bitrate(stream, seg)
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
    if not stream.segment_duration:
        stream.segment_duration = info["segment_duration"] or info["target_duration"]

    # 读第一个分片，既验证真能出流，又顺带测速
    seg_url = info["first_segment"]
    if seg_url and stream.playlist_only:
        _pace()
    seg = _fetch(client, seg_url, limit=sample_bytes, deadline=deadline) if seg_url else None
    if seg is not None and (seg.status not in _OK_STATUS or not seg.body):
        # 有些平台对同一 token 的高频请求返回 403（限流，不是死链）。这类源在
        # 采集阶段已确认列表有效，播放器按正常节奏取片不会触发，所以不当死链处理，
        # 只标记为「未测速」——保留但拿不到速率。
        if stream.playlist_only and seg.status in (403, 429):
            for _ in range(2):
                time.sleep(1.5)
                _pace()
                again = _fetch(client, seg_url, limit=sample_bytes, deadline=deadline)
                if again.status in _OK_STATUS and again.body:
                    seg = again
                    break
        if seg.status not in _OK_STATUS or not seg.body:
            if stream.playlist_only:
                stream.note = f"分片限流({seg.status})，未测速"
                if not stream.height:
                    stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name)
                stream.ok = True
                return
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
        _estimate_bitrate(stream, seg)

    if not stream.height:
        # 媒体清单本身不带分辨率（单码率直播常见），优先用采集端已知的档位
        stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name, stream.raw_name)
    stream.ok = True


def _estimate_bitrate(stream: Stream, resp) -> None:
    """用「分片大小 ÷ 分片时长」估算真实码率。

    这个数字比下载测速更有意义：测速反映 NAS 能不能拉得动，码率反映这条流
    本身有多重。1080P 直播通常在 4~8 Mbps，标清 1~2 Mbps。
    服务端没给 Content-Length（分块传输）时跳过，不瞎猜。
    """
    if stream.bandwidth:
        return
    try:
        size = int((resp.headers or {}).get("content-length") or 0)
    except (TypeError, ValueError):
        return
    if size <= 0 or stream.segment_duration <= 0:
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

    return items
