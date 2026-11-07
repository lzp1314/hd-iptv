"""结果输出：m3u / txt / json。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .models import Channel, RunReport, Stream


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免播放器读到写了一半的列表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _logo(channel: Channel, logo_base: str) -> str:
    """台标地址。

    优先用来源自带的 ``tvg-logo``，没有就指向本机的 ``/logo/<频道名>.png``：
    由服务端去台标库里取一次、存本地，播放器永远只跟本机要图，
    不会因为图床被墙或防盗链而丢台标。
    """
    for s in channel.streams:
        if s.logo:
            return s.logo
    base = (logo_base or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{quote(channel.name)}.png"


def _play_url(stream: Stream, relay_base: str) -> str:
    """有 slug 就给本机地址。

    直连地址对 UA 敏感、且有寿命；走本机下发清单后播放器用什么客户端都能放，
    地址也不会过期。没有对外地址时给相对路径——播放器按 m3u 所在地址解析，
    少数不解析相对路径的播放器由服务端现拼时补全（``/playlist.m3u`` 走的就是
    这条路径）。只有完全没有 slug 的流才退回它自己的地址。

    **有 slug 的流绝不退回 ``stream.url``**：央视频那些台的 url 是「抽测轮」
    临时换来的票，寿命只有几小时。写进列表就是一条死链——播放器打开只看到
    黑屏，而且卡片上还显示着画质和码率，很容易以为是源不好。
    """
    if stream.slug:
        base = relay_base.rstrip("/") if relay_base else ""
        if base:
            return f"{base}/live/{stream.slug}.m3u8"
        return f"/live/{stream.slug}.m3u8"
    return stream.url


def render_m3u(
    channels: list[Channel],
    *,
    epg_url: str = "",
    logo_base: str = "",
    multi_source_mark: bool = True,
    relay_base: str = "",
    tvg_ids: dict | None = None,
) -> tuple[str, int]:
    """生成 m3u 文本，返回 ``(内容, 条数)``。

    服务端拿它在每次请求时现拼，这样「对外地址」改了立刻生效，
    不用等下一轮更新把 result.m3u 重写一遍。

    ``tvg_ids`` 是「频道名 → 节目单里的频道 id」，拿到了才写得对得上节目单。
    """
    lines = []
    head = "#EXTM3U"
    if epg_url:
        # 两种写法播放器认的不一样，一起给不冲突
        head += f' x-tvg-url="{epg_url}" url-tvg="{epg_url}"'
    lines.append(head)

    count = 0
    ids = tvg_ids or {}
    for ch in channels:
        if not ch.streams:
            continue
        logo = _logo(ch, logo_base)
        seen: set[str] = set()
        for stream in ch.streams:
            # 走转发时，同一个台的多个源会塌缩成同一个地址；重复条目会让
            # 播放器在两条一模一样的记录之间来回跳，先去掉
            target = _play_url(stream, relay_base)
            if target in seen:
                continue
            seen.add(target)
            # 一个频道多个源时，在名字后面标注画质，方便播放器里区分
            suffix = ""
            if multi_source_mark and len(ch.streams) > 1 and stream.height:
                suffix = f"·{stream.grade}"
            tvg_id = ids.get(ch.name) or ch.streams[0].tvg_id or ch.name
            attrs = [
                f'tvg-id="{_attr(tvg_id)}"',
                f'tvg-name="{_attr(ch.name)}"',
                f'group-title="{_attr(ch.group)}"',
            ]
            if logo:
                attrs.append(f'tvg-logo="{_attr(logo)}"')
            lines.append(f'#EXTINF:-1 {" ".join(attrs)},{ch.name}{suffix}')
            if stream.user_agent and not (relay_base and stream.slug):
                lines.append(f"#EXTVLCOPT:http-user-agent={stream.user_agent}")
            lines.append(target)
            count += 1

    return "\n".join(lines) + "\n", count


def write_m3u(
    channels: list[Channel],
    path: Path,
    *,
    epg_url: str = "",
    logo_base: str = "",
    multi_source_mark: bool = True,
    relay_base: str = "",
    tvg_ids: dict | None = None,
) -> int:
    """生成 m3u 文件。返回写入的流条数。"""
    text, count = render_m3u(
        channels,
        epg_url=epg_url,
        logo_base=logo_base,
        multi_source_mark=multi_source_mark,
        relay_base=relay_base,
        tvg_ids=tvg_ids,
    )
    _atomic_write(path, text)
    return count


def render_txt(channels: list[Channel], *, relay_base: str = "") -> tuple[str, int]:
    """生成 txt 列表（按分组分段），返回 ``(内容, 条数)``。"""
    lines: list[str] = []
    count = 0
    current_group = None
    for ch in channels:
        if not ch.streams:
            continue
        if ch.group != current_group:
            current_group = ch.group
            lines.append(f"{ch.group},#genre#")
        for stream in ch.streams:
            lines.append(f"{ch.name},{_play_url(stream, relay_base)}")
            count += 1
    return "\n".join(lines) + "\n", count


def write_txt(channels: list[Channel], path: Path, *, relay_base: str = "") -> int:
    """生成 txt 列表文件。"""
    text, count = render_txt(channels, relay_base=relay_base)
    _atomic_write(path, text)
    return count


def write_json(channels: list[Channel], path: Path, report: RunReport | None = None) -> None:
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "report": report.as_dict() if report else {},
        "channels": [
            {
                "name": ch.name,
                "group": ch.group,
                "streams": [
                    {
                        "url": s.url,
                        # slug 必须落盘：服务端要靠它把播放地址拼成 /live/<slug>.m3u8
                        "slug": s.slug,
                        "tvg_id": s.tvg_id,
                        "height": s.height,
                        "width": s.width,
                        "grade": s.grade,
                        "codec": s.codec,
                        "bandwidth_kbps": round(s.bandwidth / 1000) if s.bandwidth else 0,
                        "speed_mbps": s.speed_mbps,
                        "delay_ms": s.delay_ms,
                        "segment_duration": round(s.segment_duration, 2),
                        "source": s.source,
                        # 本轮上游没响应、沿用上一轮条目：中转层播放时会重新换地址
                        "stale": s.stale,
                        "note": s.note,
                    }
                    for s in ch.streams
                ],
            }
            for ch in channels
            if ch.streams
        ],
    }
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _attr(value: str) -> str:
    return (value or "").replace('"', "").replace("\n", " ")


def summarize(channels: list[Channel]) -> dict:
    total = sum(len(c.streams) for c in channels)
    by_grade: dict[str, int] = {}
    for ch in channels:
        for s in ch.streams:
            by_grade[s.grade] = by_grade.get(s.grade, 0) + 1
    return {
        "channels": len([c for c in channels if c.streams]),
        "streams": total,
        "by_grade": by_grade,
    }


__all__ = ["render_m3u", "render_txt", "write_m3u", "write_txt", "write_json", "summarize", "Stream"]
