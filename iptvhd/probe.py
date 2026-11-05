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

from . import hls
from .models import Stream
from .net import HttpClient

_OK_STATUS = (200, 206)


def probe_one(stream: Stream, client: HttpClient, sample_bytes: int = 262144) -> Stream:
    """探测单条流，结果写回 stream 自身。"""
    resp = client.get(stream.url, limit=sample_bytes)
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

    # 读第一个分片，既验证真能出流，又顺带测速
    seg_url = info["first_segment"]
    seg = client.get(seg_url, limit=sample_bytes) if seg_url else None
    if seg is not None:
        if seg.status not in _OK_STATUS or not seg.body:
            stream.error = f"分片不可用: {seg.error or seg.status}"
            return
        stream.speed_mbps = seg.speed_mbps
        if seg.ttfb_ms:
            stream.delay_ms = max(stream.delay_ms, seg.ttfb_ms)

    if not stream.height:
        # 媒体清单本身不带分辨率（单码率直播常见），优先用采集端已知的档位
        stream.height = stream.hint_height or hls.guess_height(stream.url, stream.raw_name, stream.raw_name)
    stream.ok = True


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
