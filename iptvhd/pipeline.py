"""更新管线：采集 → 匹配 → 探测 → 过滤 → 输出。"""

from __future__ import annotations

import json
import os
import time
import tomllib
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import writers
from .collectors import Collector, SubscriptionCollector, build_collectors, parse_list
from .models import Channel, RunReport, Stream, need_mbps
from .net import build_client
from .normalize import ChannelMatcher, load_channels
from .probe import probe_gentle, probe_many
from .relay import needs_relay, static_slug
from .settings import Settings, upgrade_channels, upgrade_sources

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


# --------------------------------------------------------------------------- #
# 低频抽测：默认零请求防限流，但每隔几轮做一次真实测速，把码率/速率缓存起来，
# 让面板能持续显示「码率/测速」而不是永远「未测速」。
# --------------------------------------------------------------------------- #
_MEASURE_FILE = "measure.json"          # 抽测结果缓存，落在 output 目录下
_MEASURE_EVERY = 4                       # 每 4 轮抽测一次（config 可覆盖）


def _measure_path(settings: Settings) -> Path:
    return settings.data_dir / _MEASURE_FILE


def _load_measure(settings: Settings) -> dict:
    try:
        data = json.loads(_measure_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_measure(settings: Settings, data: dict) -> None:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _measure_path(settings).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _measure_every(settings: Settings) -> int:
    try:
        return max(1, int(settings.get("probe", "measure_every", _MEASURE_EVERY)))
    except (TypeError, ValueError):
        return _MEASURE_EVERY


def _is_measure_round(settings: Settings) -> bool:
    """这一轮要不要做真实抽测。

    用一个计数文件记录轮次：每轮 +1，``round % every == 0`` 时抽测。
    计数文件不存在时视为第 1 轮（立即抽一次，好让面板第一次就有数据）。
    """
    counter = settings.data_dir / "measure_round"
    try:
        round_no = int(counter.read_text(encoding="utf-8").strip() or "0") + 1
    except (OSError, ValueError):
        round_no = 1
    try:
        counter.write_text(str(round_no), encoding="utf-8")
    except OSError:
        pass
    return round_no % _measure_every(settings) == 0


def _enable_measure(sources: list[dict]) -> list[dict]:
    """抽测轮：给主源临时打开 resolve + verify_segment，采集阶段会去取地址并读分片测速。

    只改内存里的副本，不落盘——下一轮自动回到零请求模式，不会让限流常态化。
    """
    out: list[dict] = []
    for item in sources:
        item = dict(item)
        if str(item.get("type", "")).lower() == "yangshipin":
            item["resolve"] = True
            item["verify_segment"] = True
        out.append(item)
    return out


def _apply_measure_cache(streams: list, settings: Settings) -> None:
    """非抽测轮：把上一轮缓存下来的测速值按 slug 回填到 Stream 上。

    回填的是「上一次真实测速」的结果，可能已经过了一段时间，所以标个 note
    说明是历史抽测值；面板照常显示码率/测速，不用永远「未测速」。
    """
    cache = _load_measure(settings)
    if not cache:
        return
    for s in streams:
        if not s.slug:
            continue
        row = cache.get(s.slug)
        if not row:
            continue
        if s.bandwidth <= 0:
            s.bandwidth = int(row.get("bandwidth") or 0)
        if s.speed_mbps <= 0:
            s.speed_mbps = float(row.get("speed_mbps") or 0.0)
        if s.delay_ms <= 0:
            s.delay_ms = int(row.get("delay_ms") or 0)
        if (s.bandwidth or s.speed_mbps) and not s.note:
            s.note = "历史抽测"


def _extract_measure(streams: list, settings: Settings) -> None:
    """抽测轮：把这一轮真实测到的码率/速率按 slug 存进缓存，供后续轮次回填。"""
    cache = _load_measure(settings)
    for s in streams:
        if not s.slug:
            continue
        if not (s.bandwidth or s.speed_mbps):
            continue
        cache[s.slug] = {
            "bandwidth": s.bandwidth,
            "speed_mbps": s.speed_mbps,
            "delay_ms": s.delay_ms,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    _save_measure(settings, cache)


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
            # 失败时的诊断信息（比如镜像挨个试都没通）比错误本身更有用
            for note in getattr(collector, "notes", []) or []:
                log(f"    {note}")
    return streams, stats


def _dedupe(streams: list[Stream]) -> list[Stream]:
    seen: set[str] = set()
    out: list[Stream] = []
    for s in streams:
        # 采集阶段可以只给 slug、不给地址（地址在播放时现换），这种条目按 slug 认
        key = s.url.strip() or (f"slug:{s.slug}" if s.slug else "")
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
        # 显示名匹配不上时退回 tvg-name：有的列表显示名带长后缀，反而对不上
        if result.channel is None and s.alt_name:
            result = matcher.match(s.alt_name)
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
        if s.speed_mbps:
            # 门槛按画质分档，不再一刀切 0.8 Mbps：那个数字连 480P 都勉强，
            # 1080P 标称 1.33 Mbps 的源照样放行，结果就是播一卡一。
            # 取「该档画质的一半」当底线——留出线路波动的余量，又不至于把
            # 1.5~2 Mbps 这类凑合能看的源全砍光（砍光就只能回落央视频换票）。
            floor = max(min_speed, need_mbps(s.height) * 0.5)
            if s.speed_mbps < floor:
                continue
        kept.append(s)
    return kept


# 同一个最终地址上出现几个不同的台，就判定为广告农场 / 占位源。
# 取 3 而不是 2：两个台偶尔撞一次可能是源作者写错，三个以上只能是故意的。
_SHARED_ENDPOINT_CHANNELS = 3


def _endpoint(url: str) -> str:
    """把地址削成 ``scheme://host/path``，丢掉 query。

    必须丢 query：广告农场给每个频道发的签到参数都不一样（``?app=cdnhk08``、
    ``?app=cdnhk09``），比整条 URL 一条都比不中。
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    return f"{parts.scheme}://{parts.netloc}{parts.path}".lower()


def _drop_shared_endpoints(streams: list[Stream], log: Logger) -> int:
    """把「好几个不同的台落到同一个最终地址」的源整批剔除。

    央视那批 720P 广告源就是这么漏进来的：原始地址各不相同
    （``/cdnlive/cctv4.m3u8``、``/cdnlive/cctv5.m3u8`` …），按 URL 去重一条都抓
    不到，但它们 302 之后全落到同一个 ``/applive/mkt.m3u8``——同一路购物直播。
    一个真实频道不可能跟别的台共用一路流，所以「一址多台」就是广告/占位的指纹。

    只比对 302 之后的**最终**地址，不认任何写死的域名关键字：换个广告农场、
    换套参数也照样能抓出来。
    """
    endpoints: dict[str, set[str]] = {}
    for s in streams:
        if s.final_url:
            endpoints.setdefault(_endpoint(s.final_url), set()).add(s.channel)

    bad = {ep for ep, names in endpoints.items() if len(names) >= _SHARED_ENDPOINT_CHANNELS}
    if not bad:
        return 0

    dropped = 0
    for s in streams:
        if s.final_url and _endpoint(s.final_url) in bad:
            s.ok = False
            s.error = "同一个地址被多个台共用（广告/占位源）"
            dropped += 1
    if dropped:
        log(f"[剔除] {dropped} 条源落在同一个购物/占位地址上，已整批丢弃（{len(bad)} 个落点）")
    return dropped


def _probe_candidates(
    candidates: list[Stream], client, settings: Settings, log: Logger
) -> None:
    """探测候选，分两拨打：聚合源并发，运营商官方源串行。

    官方源（移动/电信 IPTV 的 ``1700xxxxxx`` 账号）按出口 IP 记账，并发几十条
    清单请求就会被限成「302 无限循环」，之后连单条都取不到。所以两拨必须分开，
    不能让 200 多条聚合源把它一起冲掉。
    """
    official = [s for s in candidates if s.trusted]
    others = [s for s in candidates if not s.trusted]

    if others:
        log(
            f"[探测] 开始并发探测 {len(others)} 条（并发 {settings.concurrency}，"
            f"单条上限 {settings.probe_timeout:.0f}s，整批预算 {settings.probe_budget:.0f}s）"
        )

        def on_progress(done: int, total: int) -> None:
            if done == total or done % max(1, total // 10) == 0:
                log(f"[探测] {done}/{total}")

        def on_stall(pending: int, seconds: float) -> None:
            # 长尾阻塞的实时提示：以前面板停在「297/332」就再没动静，只能干等
            log(f"! [探测] {pending} 条已 {seconds:.0f}s 没有返回（慢源，到点会丢弃）")

        probe_many(
            others,
            client,
            concurrency=settings.concurrency,
            sample_bytes=settings.sample_bytes,
            per_stream_timeout=settings.probe_timeout,
            budget=settings.probe_budget,
            on_progress=on_progress,
            on_stall=on_stall,
        )

    if official:
        try:
            gap = float(settings.get("probe", "official_gap", 1.2))
        except (TypeError, ValueError):
            gap = 1.2
        log(
            f"[探测·官方源] {len(official)} 条串行慢探（每条间隔 {gap:.1f}s）"
            "——这路一并发就会被 CDN 限成 302 死循环"
        )

        def on_official(done: int, total: int) -> None:
            if done == total or done % 8 == 0:
                log(f"[探测·官方源] {done}/{total}")

        aborted = probe_gentle(
            official,
            client,
            sample_bytes=settings.sample_bytes,
            # 官方源是本省边缘，正常几毫秒就回；单条上限压到 10 秒，
            # 免得 50 多个台里混进几条卡住的，把整轮拖成十几分钟
            per_stream_timeout=min(settings.probe_timeout or 10.0, 10.0),
            gap=gap,
            on_progress=on_official,
        )
        if aborted:
            log("! [探测·官方源] 失败率过半，判定账号已被限流，本轮整批旁路（下一轮再试）")


def _route_official_through_relay(streams: list[Stream]) -> int:
    """把「上游在 IPv6 上」的静态源改成走本机转发，返回改了几条。

    运营商 IPTV 的边缘只下发 IPv6 单播地址。播放器（电视盒子、老版本 VLC）
    一大半没有 IPv6，直连必报「格式不支持」——而 302 前的原始地址看着完全正常，
    排查时最容易误判成播放器的问题。这类源一律由本机中转：清单和分片都过 NAS，
    播放端有没有 IPv6 就无所谓了。

    做法是把地址编进 slug（见 relay.static_slug），输出列表里就成了
    ``/live/<slug>.m3u8``，中转层拿 slug 里的地址去取，不用注册表。

    ``trusted`` 的源无条件走这条路，不等探测记下最终地址：探测被跳过、或者这
    一轮只拿到 302 头没跟到底，都会漏掉，而漏掉的后果就是「这个台放不出来」。
    """
    routed = 0
    for s in streams:
        if s.slug or not s.url:
            continue
        if s.trusted or needs_relay(s.final_url or s.url):
            s.slug = static_slug(s.url)
            routed += 1
    return routed


def run_update(settings: Settings, log: Logger | None = None) -> RunReport:
    log = log or _noop
    report = RunReport(started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    started = time.monotonic()

    # 容器里挂着的旧默认模板不会随镜像更新，这里补一刀（用户改过的不动）
    if upgrade_channels(settings.channels_file):
        log(f"[模板] 旧版默认频道模板已换成新版：{settings.channels_file}")
    # 同理：旧默认采集源（走央视频接口，会被按 IP 限流）换成静态开放源。
    # 不换的话，宿主机上那份旧 sources.toml 会一直盖着镜像里的新配置，
    # 删容器重建也没用——起来的还是那个被限流的源。
    if upgrade_sources(settings.sources_file):
        log(f"[源] 旧版默认采集源已换成新版（含官方直连）：{settings.sources_file}")
    if settings.config_upgraded:
        log("[配置] 画质上限已从 1080 放开到 4K —— 官方源常常只有 4K 一路")

    channels = load_channels(settings.channels_file)
    report.channels_total = len(channels)
    if not channels:
        log(f"! 频道模板为空：{settings.channels_file}")
        return report
    log(f"[模板] 载入 {len(channels)} 个频道")

    sources = load_sources(settings)
    measure_round = _is_measure_round(settings)
    if measure_round:
        # 抽测轮：主源临时打开 resolve + verify_segment，去取真实地址并测速。
        # 这一轮会多打几十个上游请求，但每 N 轮才一次，不会让限流常态化。
        sources = _enable_measure(sources)
        log(f"[抽测] 本轮开启真实测速（每 {_measure_every(settings)} 轮一次）")
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
        client = build_client(
            user_agent=settings.user_agent,
            timeout=settings.timeout,
            retries=1,  # 探测阶段不做重试，靠并发量换速度
            proxy=settings.proxy,
        )
        _probe_candidates(candidates, client, settings, log)
    else:
        for s in candidates:
            s.ok = True

    _drop_shared_endpoints(candidates, log)
    alive = _filter([s for s in candidates], settings)
    report.alive = len(alive)

    # 抽测数据落地：抽测轮把真实测速结果存进缓存；非抽测轮把历史测速回填，
    # 让面板持续显示码率/测速而不是永远「未测速」。
    if measure_round:
        _extract_measure(alive, settings)
    else:
        _apply_measure_cache(alive, settings)

    log(
        f"[过滤] 可用 {len(alive)} 条"
        f"（画质 {settings.min_height}~{settings.max_height}P，速率门槛 {settings.min_speed_mbps}Mbps）"
    )

    kept_by_channel: dict[str, list[Stream]] = {}
    _route_official_through_relay(alive)
    for s in alive:
        kept_by_channel.setdefault(s.channel, []).append(s)

    matched = 0
    for ch in channels:
        ch.streams = kept_by_channel.get(ch.name, [])
        if ch.streams:
            ch.accept(settings.max_per_channel)
            matched += 1
    report.channels_matched = matched

    # 这一轮被上游 403 打下来的台，只要上一轮还是好的就别让它消失
    carried = _carry_over(settings, channels, log)
    if carried:
        report.channels_matched = matched + carried

    report.missing = _missing(channels)
    if report.missing:
        names = "、".join(row["name"] for row in report.missing[:10])
        log(f"[缺失] {len(report.missing)} 个台没有流：{names}" + ("…" if len(report.missing) > 10 else ""))

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
        lines_per_channel=settings.lines_per_channel,
    )
    writers.write_txt(channels, txt_path, relay_base=settings.public_url,
                      lines_per_channel=settings.lines_per_channel)
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


def _carry_over(settings: Settings, channels: list[Channel], log: Logger) -> int:
    """本轮没取到的台，把上一轮能用的条目补回来。

    上游是按出口 IP 限流的，同一轮里总有七八个台被 403 打下来。这些台上一轮明明
    是好的，直接从列表里抹掉的话，播放器里就整台消失——那正是「好多台根本没拉
    取到」的观感。

    列表里的地址是中转地址（/live/<slug>.m3u8），播放时由中转层**实时**换票，
    所以这里只要保住「这个台存在」这一条就够了，旧地址过不过期都不影响。
    上一轮本身就有的台才补，源里从来没有的台不会凭空多出一个点进去报错的条目。
    """
    prev = load_result(settings) or {}
    old_rows = prev.get("channels") or []
    if not old_rows:
        return 0

    generated = str(prev.get("generated_at") or "")
    try:
        age_h = (datetime.now() - datetime.strptime(generated, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except ValueError:
        age_h = 0.0
    if age_h > _CARRY_MAX_HOURS:
        log(f"[继承] 上一轮结果已是 {age_h:.1f} 小时前，地址大概率失效，跳过")
        return 0

    old = {str(c.get("name") or ""): c for c in old_rows}
    limit = settings.max_per_channel
    filled: list[str] = []
    for ch in channels:
        if ch.streams:
            continue
        row = old.get(ch.name)
        if not row:
            continue
        streams: list[Stream] = []
        for item in row.get("streams") or []:
            slug = str(item.get("slug") or "").strip()
            if not slug:
                continue
            # 落盘用的是 bandwidth_kbps，读回来换算成 bps
            kbps = int(item.get("bandwidth_kbps") or 0)
            streams.append(
                Stream(
                    url=str(item.get("url") or ""),
                    raw_name=ch.name,
                    group=ch.group,
                    slug=slug,
                    tvg_id=str(item.get("tvg_id") or ""),
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                    bandwidth=int(item.get("bandwidth") or kbps * 1000),
                    codec=str(item.get("codec") or ""),
                    segment_duration=float(item.get("segment_duration") or 0.0),
                    playlist_only=True,
                    stale=True,
                    ok=True,
                    note="本轮上游未响应，播放时重新换地址",
                )
            )
            if len(streams) >= limit:
                break
        if streams:
            ch.streams = streams
            filled.append(ch.name)

    if filled:
        log(f"[继承] {len(filled)} 个台本轮没取到，沿用上一轮条目：" + "、".join(filled[:12])
            + ("…" if len(filled) > 12 else ""))
    return len(filled)


_CARRY_MAX_HOURS = 6.0


def _covered_by_upstream(name: str, keys: list[str]) -> bool:
    """模板里的名字，上游有没有对应频道。

    两边都归一化后再比，而且要能双向容纳：模板写 CCTV-1、源里叫 cctv1综合，
    模板的 key 是源 key 的前缀。数字边界要卡住，否则 cctv1 会把 cctv11 也算上。
    """
    from .normalize import normalize

    key = normalize(name)
    if not key:
        return False
    for other in keys:
        if other == key:
            return True
        for short, long_ in ((key, other), (other, key)):
            if not long_.startswith(short):
                continue
            rest = long_[len(short):]
            if not rest[:1].isdigit():
                return True
    return False


def _missing(channels: list[Channel]) -> list[dict]:
    """哪些台这轮没拿到，以及原因。

    区分「上游根本没有这个台」和「这轮被限流打下来」很重要：前者该从模板里删掉，
    后者等下一轮就好。混在一起说「有 10 个台缺失」，用户只能干瞪眼。
    """
    empty = [ch for ch in channels if not ch.streams]
    if not empty:
        return []
    try:
        from . import ysp
        from .normalize import normalize

        keys = [normalize(row[1]) for row in ysp.CHANNELS]
        keys = [k for k in keys if k]
    except Exception:  # noqa: BLE001
        keys = []

    out: list[dict] = []
    for ch in empty:
        unknown = bool(keys) and not _covered_by_upstream(ch.name, keys)
        out.append(
            {
                "name": ch.name,
                "group": ch.group,
                "reason": "上游没有这个台" if unknown else "本轮没取到（限流或临时故障）",
            }
        )
    return out


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
