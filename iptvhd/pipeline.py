"""更新管线：采集 → 匹配 → 探测 → 过滤 → 输出。"""

from __future__ import annotations

import os
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import writers
from .collectors import Collector, SubscriptionCollector, build_collectors, parse_list
from .models import Channel, RunReport, Stream
from .net import build_client
from .normalize import ChannelMatcher, load_channels
from .probe import probe_many
from .settings import Settings

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def load_sources(settings: Settings) -> list[dict]:
    sources: list[dict] = []
    path = settings.sources_file
    if path.exists():
        with path.open("rb") as fp:
            data = tomllib.load(fp)
        raw = data.get("source", [])
        if isinstance(raw, list):
            sources.extend(x for x in raw if isinstance(x, dict))

    # 环境变量里追加订阅源，便于 CI / Secret 注入
    extra = os.environ.get("HDIPTV_SUBSCRIBE", "").strip()
    if extra:
        for line in extra.replace(",", "\n").splitlines():
            url = line.strip()
            if url:
                sources.append({"name": f"env-{len(sources)}", "type": "subscription", "url": url})

    # 本地源的相对路径统一按项目根目录解析，避免受工作目录影响
    for item in sources:
        if str(item.get("type", "")).lower() == "local" and item.get("path"):
            p = Path(str(item["path"]))
            if not p.is_absolute():
                item["path"] = str(settings.root / p)
    return sources


def _collect(settings: Settings, collectors: list[Collector], log: Logger) -> tuple[list[Stream], list[dict]]:
    client = build_client(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        retries=settings.retries,
        proxy=settings.proxy,
    )
    streams: list[Stream] = []
    stats: list[dict] = []
    for collector in collectors:
        started = time.monotonic()
        try:
            got = collector.collect(client)
            streams.extend(got)
            stats.append(
                {
                    "name": collector.name,
                    "type": collector.kind,
                    "count": len(got),
                    "elapsed": round(time.monotonic() - started, 2),
                    "error": "",
                }
            )
            log(f"[采集] {collector.name}（{collector.kind}）→ {len(got)} 条")
            for note in getattr(collector, "notes", []) or []:
                log(f"    跳过 {note}")
        except Exception as exc:  # noqa: BLE001 - 单个源失败不影响其它源
            stats.append(
                {
                    "name": collector.name,
                    "type": collector.kind,
                    "count": 0,
                    "elapsed": round(time.monotonic() - started, 2),
                    "error": str(exc),
                }
            )
            log(f"[采集] {collector.name} 失败：{exc}")
    return streams, stats


def _dedupe(streams: list[Stream]) -> list[Stream]:
    seen: set[str] = set()
    out: list[Stream] = []
    for s in streams:
        key = s.url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _assign(streams: list[Stream], channels: list[Channel], matcher: ChannelMatcher, log: Logger) -> tuple[dict[str, list[Stream]], int]:
    by_channel: dict[str, list[Stream]] = {ch.name: [] for ch in channels}
    unmatched = 0
    for s in streams:
        if not s.raw_name and s.group:
            s.raw_name = s.group  # 少数列表把频道名写在分组里
        result = matcher.match(s.raw_name)
        if result.channel is None:
            unmatched += 1
            continue
        s.channel = result.channel.name
        s.channel_group = result.channel.group
        by_channel[result.channel.name].append(s)
    log(f"[匹配] 命中 {len(streams) - unmatched}/{len(streams)} 条，未匹配 {unmatched} 条")
    return by_channel, unmatched


def _filter(streams: list[Stream], settings: Settings) -> list[Stream]:
    min_h = settings.min_height
    max_h = settings.max_height
    min_speed = settings.min_speed_mbps
    allow_unknown = settings.allow_unknown
    kept: list[Stream] = []
    for s in streams:
        if not s.ok:
            continue
        if s.height:
            if s.height < min_h:
                continue
            # 画质上限：4K 对家用带宽和盒子的负担远大于收益，默认砍掉
            if max_h and s.height > max_h:
                continue
        elif not allow_unknown:
            continue
        if s.speed_mbps and s.speed_mbps < min_speed:
            continue
        kept.append(s)
    return kept


def run_update(settings: Settings, log: Logger | None = None) -> RunReport:
    log = log or _noop
    report = RunReport(started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    started = time.monotonic()

    channels = load_channels(settings.channels_file)
    report.channels_total = len(channels)
    if not channels:
        log(f"! 频道模板为空：{settings.channels_file}")
        return report
    log(f"[模板] 载入 {len(channels)} 个频道")

    sources = load_sources(settings)
    collectors = build_collectors(sources)
    if not collectors:
        log("! 没有启用的采集源，请检查 config/sources.toml")
        return report

    raw_streams, source_stats = _collect(settings, collectors, log)
    report.sources = source_stats
    report.collected = len(raw_streams)
    if not raw_streams:
        report.published = _previous_published(settings)
        log("! 采集结果为空，保留上一次的列表，终止")
        return report

    streams = _dedupe(raw_streams)
    report.collected = len(streams)
    log(f"[去重] {len(raw_streams)} → {len(streams)} 条")

    matcher = ChannelMatcher(channels)
    by_channel, _ = _assign(streams, channels, matcher, log)

    candidates = [s for group in by_channel.values() for s in group]
    report.probed = len(candidates)
    if candidates and bool(settings.get("probe", "enabled", True)):
        log(f"[探测] 开始并发探测 {len(candidates)} 条（并发 {settings.concurrency}）")

        def on_progress(done: int, total: int) -> None:
            if done == total or done % max(1, total // 10) == 0:
                log(f"[探测] {done}/{total}")

        client = build_client(
            user_agent=settings.user_agent,
            timeout=settings.timeout,
            retries=1,  # 探测阶段不做重试，靠并发量换速度
            proxy=settings.proxy,
        )
        probe_many(
            candidates,
            client,
            concurrency=settings.concurrency,
            sample_bytes=settings.sample_bytes,
            on_progress=on_progress,
        )
    else:
        for s in candidates:
            s.ok = True

    alive = _filter([s for s in candidates], settings)
    report.alive = len(alive)
    log(
        f"[过滤] 可用 {len(alive)} 条"
        f"（画质 {settings.min_height}~{settings.max_height}P，速率门槛 {settings.min_speed_mbps}Mbps）"
    )

    kept_by_channel: dict[str, list[Stream]] = {}
    for s in alive:
        kept_by_channel.setdefault(s.channel, []).append(s)

    matched = 0
    for ch in channels:
        ch.streams = kept_by_channel.get(ch.name, [])
        if ch.streams:
            ch.accept(settings.max_per_channel)
            matched += 1
    report.channels_matched = matched

    out_dir = settings.data_dir
    m3u_path = out_dir / "result.m3u"
    txt_path = out_dir / "result.txt"
    json_path = out_dir / "result.json"

    summary = writers.summarize(channels)
    report.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report.elapsed = time.monotonic() - started

    prev_published = _previous_published(settings)
    if summary["streams"] == 0 and prev_published > 0:
        # 上游集体抽风或被限流时也会走到这里。宁可留着上一轮能用的列表，
        # 也不能把它覆盖成空文件——那会让正在播的客户端当场全断。
        report.published = prev_published
        log("! 本轮 0 条可用流，保留上一次的列表不覆盖，下一轮再试")
        return report

    report.published = writers.write_m3u(
        channels,
        m3u_path,
        epg_url=settings.epg_url,
        logo_base=settings.logo_base,
        relay_base=settings.public_url,
    )
    writers.write_txt(channels, txt_path, relay_base=settings.public_url)
    writers.write_json(channels, json_path, report)
    writers.write_json(channels, out_dir / "latest.json", report)
    log(
        f"[完成] {summary['channels']} 个频道 / {summary['streams']} 条流，"
        f"画质分布 {summary['by_grade']}，耗时 {report.elapsed:.1f}s"
    )
    log(f"[输出] {m3u_path}")
    if not summary["streams"]:
        log("! 没有可用结果：检查探测门槛、模板频道名是否与源一致")
    return report


def _previous_published(settings: Settings) -> int:
    """上一轮发布了几条，用于判断能不能拿旧列表顶着。"""
    try:
        return int((load_result(settings).get("report") or {}).get("published") or 0)
    except (TypeError, ValueError):
        return 0


def load_result(settings: Settings) -> dict:
    path = settings.data_dir / "latest.json"
    if not path.exists():
        return {}
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["run_update", "load_sources", "load_result", "SubscriptionCollector", "parse_list", "Path"]
