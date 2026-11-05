"""HLS 播放列表解析。

直播源的分辨率信息几乎都写在 m3u8 主播放列表的 #EXT-X-STREAM-INF 里，
读一下就有，不需要拉起 ffmpeg 解码。这是本项目判「高清」的主要手段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

_QUALITY_HINT = re.compile(r"(?i)(?<![a-z0-9])(2160p?|4k|1440p?|2k|1080p?|720p?|576p?|480p?)(?![a-z0-9])")

_HINT_HEIGHT = {
    "2160p": 2160, "2160": 2160, "4k": 2160,
    "1440p": 1440, "1440": 1440, "2k": 1440,
    "1080p": 1080, "1080": 1080,
    "720p": 720, "720": 720,
    "576p": 576, "576": 576,
    "480p": 480, "480": 480,
}


@dataclass(slots=True)
class Variant:
    uri: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    frame_rate: float = 0.0

    @property
    def pixels(self) -> int:
        return self.width * self.height


def is_playlist(text: str) -> bool:
    head = text.lstrip()[:64]
    return head.startswith("#EXTM3U")


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def _split_attrs(line: str) -> dict:
    """解析 `KEY=VAL,KEY="A,B"` 形式的属性串，逗号在引号内不切分。"""
    attrs: dict[str, str] = {}
    key, buf, quoted, has_key = "", "", False, False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            buf += ch
        elif ch == "," and not quoted:
            if has_key:
                attrs[key.strip().upper()] = buf.strip().strip('"')
            key, buf, has_key = "", "", False
        elif ch == "=" and not quoted and not has_key:
            key, has_key = buf.strip(), True
            buf = ""
        else:
            buf += ch
    if has_key:
        attrs[key.strip().upper()] = buf.strip().strip('"')
    return attrs


def parse_variants(text: str, base_url: str = "") -> list[Variant]:
    """从主播放列表里取出所有码率变体。"""
    variants: list[Variant] = []
    lines = [ln.strip() for ln in text.splitlines()]
    for idx, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _split_attrs(line.split(":", 1)[1])
        uri = ""
        for nxt in lines[idx + 1:]:
            if not nxt or nxt.startswith("#"):
                continue
            uri = nxt
            break
        if not uri:
            continue
        width = height = 0
        res = attrs.get("RESOLUTION", "")
        if "x" in res.lower():
            try:
                w_s, h_s = res.lower().split("x", 1)
                width, height = int(w_s), int(h_s)
            except ValueError:
                pass
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
        except ValueError:
            bandwidth = 0
        try:
            frame_rate = float(attrs.get("FRAME-RATE", "0") or 0)
        except ValueError:
            frame_rate = 0.0
        variants.append(
            Variant(
                uri=urljoin(base_url, uri),
                bandwidth=bandwidth,
                width=width,
                height=height,
                codecs=(attrs.get("CODECS", "") or "").lower(),
                frame_rate=frame_rate,
            )
        )
    return variants


def best_variant(variants: list[Variant]) -> Variant | None:
    if not variants:
        return None
    return max(variants, key=lambda v: (v.height, v.bandwidth, v.pixels))


def parse_media(text: str, base_url: str = "") -> dict:
    """媒体播放列表的基础信息，用于判断是否真能出流。"""
    info: dict = {
        "target_duration": 0.0,
        "segments": 0,
        "encrypted": False,
        "first_segment": "",
        "segment_duration": 0.0,
    }
    extinf = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                info["target_duration"] = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line.upper():
            info["encrypted"] = True
        elif line.startswith("#EXTINF:"):
            try:
                extinf = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                extinf = 0.0
        elif line and not line.startswith("#"):
            info["segments"] += 1
            if not info["first_segment"]:
                info["first_segment"] = urljoin(base_url, line)
    info["segment_duration"] = extinf
    return info


def guess_height(*texts: str) -> int:
    """从 URL / 频道名里猜分辨率，作为拿不到 RESOLUTION 时的兜底。"""
    for text in texts:
        if not text:
            continue
        for match in _QUALITY_HINT.finditer(text):
            height = _HINT_HEIGHT.get(match.group(1).lower())
            if height:
                return height
    return 0
