"""结果输出：m3u / txt / json。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

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
    if not logo_base:
        return ""
    for s in channel.streams:
        if s.logo:
            return s.logo
    return f"{logo_base.rstrip('/')}/{channel.name}.png"


def _play_url(stream: Stream, relay_base: str) -> str:
    """优先给本机转发地址。

    直连地址对 UA 敏感、且有寿命；走本机转发后播放器用什么客户端都能放，
    地址也不会过期。``relay_base`` 为空时退回直连，便于离线导出。
    """
    if relay_base and stream.slug:
        return f"{relay_base.rstrip('/')}/live/{stream.slug}.m3u8"
    return stream.url


def write_m3u(
    channels: list[Channel],
    path: Path,
    *,
    epg_url: str = "",
    logo_base: str = "",
    multi_source_mark: bool = True,
    relay_base: str = "",
) -> int:
    """生成 m3u。返回写入的流条数。"""
    lines = []
    head = "#EXTM3U"
    if epg_url:
        head += f' x-tvg-url="{epg_url}"'
    lines.append(head)

    count = 0
    for ch in channels:
        if not ch.streams:
            continue
        logo = _logo(ch, logo_base)
        seen: set[str] = set()
        for idx, stream in enumerate(ch.streams, start=1):
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
            tvg_id = ch.streams[0].tvg_id or ch.name
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

    _atomic_write(path, "\n".join(lines) + "\n")
    return count


def write_txt(channels: list[Channel], path: Path, *, relay_base: str = "") -> int:
    """生成 txt 列表（按分组分段）。"""
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
    _atomic_write(path, "\n".join(lines) + "\n")
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
                        "height": s.height,
                        "width": s.width,
                        "grade": s.grade,
                        "codec": s.codec,
                        "bandwidth_kbps": round(s.bandwidth / 1000) if s.bandwidth else 0,
                        "speed_mbps": s.speed_mbps,
                        "delay_ms": s.delay_ms,
                        "source": s.source,
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


__all__ = ["write_m3u", "write_txt", "write_json", "summarize", "Stream"]
