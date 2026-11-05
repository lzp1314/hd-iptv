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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from . import hls, videoinfo
from .models import Stream
from .net import HttpClient

_OK_STATUS = (200, 206)

# 全局节拍器：对「热门接口」类源（央视频）限速，避免并发探测触发对方的
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


def _probe_prefetched(stream: Stream, client: HttpClient, sample_bytes: int) -> Stream:
    """采集阶段已经验证过列表、并留下首个分片地址的流。

    省掉一次列表请求，直接读分片测速；被限流时仍保留，只标记未测速。
    """
    from . import ysp

    extra = {"User-Agent": stream.user_agent} if stream.user_agent else None
    ysp.pace()
    seg = client.get(stream.first_segment, limit=sample_bytes, headers=extra)
    if seg.status in (403, 429):
        for _ in range(2):
            time.sleep(1.5)
            ysp.pace()
            seg = client.get(stream.first_segment, limit=sample_bytes, headers=extra)
            if seg.status not in (403, 429):
                break

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


def probe_one(stream: Stream, client: HttpClient, sample_bytes: int = 262144) -> Stream:
    """探测单条流，结果写回 stream 自身。

    部分平台（央视频）要求特定 UA 才放行分片，所以流自带 UA 时优先用它。
    """
    if stream.playlist_only and stream.first_segment:
        return _probe_prefetched(stream, client, sample_bytes)

    extra = {"User-Agent": stream.user_agent} if stream.user_agent else None
    if stream.playlist_only:
        _pace()
    resp = client.get(stream.url, limit=sample_bytes, headers=extra)
    if stream.playlist_only and resp.status in (403, 429):
        # 这类源对突发请求敏感，退避后再试几次，别把限流误判成死链
        for _ in range(3):
            time.sleep(2.0)
            _pace()
            resp = client.get(stream.url, limit=sample_bytes, headers=extra)
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
        _probe_hls(stream, client, body_text, resp, sample_bytes)
        return stream

    # 非播放列表：直连 ts/flv/mp4，能下到字节即可认为可用
    stream.ok = bool(resp.body)
    stream.speed_mbps = resp.speed_mbps
    stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name)
    if not stream.ok:
        stream.error = "响应体为空"
    return stream


def _probe_hls(stream: Stream, client: HttpClient, body_text: str, resp, sample_bytes: int) -> None:
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
        sub = client.get(variant.uri, limit=65536)
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
    seg = client.get(seg_url, limit=sample_bytes) if seg_url else None
    if seg is not None and (seg.status not in _OK_STATUS or not seg.body):
        # 有些平台对同一 token 的高频请求返回 403（限流，不是死链）。这类源在
        # 采集阶段已确认列表有效，播放器按正常节奏取片不会触发，所以不当死链处理，
        # 只标记为「未测速」——保留但拿不到速率。
        if stream.playlist_only and seg.status in (403, 429):
            for _ in range(2):
                time.sleep(1.5)
                _pace()
                again = client.get(seg_url, limit=sample_bytes)
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


def probe_many(
    streams: Iterable[Stream],
    client: HttpClient,
    *,
    concurrency: int = 24,
    sample_bytes: int = 262144,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Stream]:
    """并发探测，保持输入顺序返回。"""
    items = list(streams)
    total = len(items)
    if not total:
        return []

    done = 0
    lock = threading.Lock()
    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(probe_one, s, client, sample_bytes): s for s in items}
        for fut in as_completed(futures):
            stream = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001 - 单条流异常不能中断整批
                stream.ok = False
                stream.error = f"探测异常: {type(exc).__name__}: {exc}"
            with lock:
                done += 1
                if on_progress:
                    on_progress(done, total)

    _ = time.monotonic() - started
    return items
