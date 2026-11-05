"""频道名清洗与匹配。

直播源里的频道名千奇百怪：`湖南卫视_HD`、`湖南卫视FHD`、`湖南衛視`、`CCTV1综合`、
`CCTV-1 1080P 50FPS`。这里把它们归一化后再和模板比对，匹配不上的直接丢弃，
避免把「CCTV-1」的流塞进「CCTV-11」。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .models import Channel

# 常用繁体 → 简体（覆盖电视台名称里高频出现字）
_TRAD = {
    "視": "视", "臺": "台", "灣": "湾", "廣": "广", "東": "东", "衛": "卫",
    "綜": "综", "藝": "艺", "劇": "剧", "場": "场", "紀": "纪", "錄": "录",
    "體": "体", "育": "育", "財": "财", "經": "经", "兒": "儿", "婦": "妇",
    "軍": "军", "農": "农", "業": "业", "國": "国", "際": "际", "聞": "闻",
    "華": "华", "僑": "侨", "電": "电", "影": "影", "戲": "戏", "曲": "曲",
    "樂": "乐", "娛": "娱", "遊": "游", "戲": "戏", "書": "书", "畫": "画",
    "紀": "纪", "實": "实", "選": "选", "購": "购", "車": "车", "產": "产",
    "網": "网", "絡": "络", "數": "数", "碼": "码", "級": "级", "獎": "奖",
    "門": "门", "陽": "阳", "龍": "龙", "遼": "辽", "寧": "宁", "貴": "贵",
    "雲": "云", "陝": "陕", "隴": "陇", "瓊": "琼", "蘇": "苏", "浙": "浙",
    "贛": "赣", "閩": "闽", "粵": "粤", "滬": "沪", "漢": "汉", "廈": "厦",
    "濱": "滨", "鄭": "郑", "濟": "济", "長": "长", "沈": "沈", "撫": "抚",
    "順": "顺", "慶": "庆", "錦": "锦", "鐵": "铁", "嶺": "岭", "峽": "峡",
    "稱": "称", "頌": "颂", "鳳": "凤", "鷹": "鹰", "鳥": "鸟", "魚": "鱼",
    "藏": "藏", "肅": "肃", "遜": "逊", "爾": "尔", "齊": "齐", "魯": "鲁",
    "晉": "晋", "冀": "冀", "豫": "豫", "鄂": "鄂", "湘": "湘", "皖": "皖",
}

# 画质/编码/帧率等无关标记
_NOISE = re.compile(
    r"(?i)(?<![a-z0-9])("
    r"uhd|fhd|hd|sd|hdr|hevc|h\.?26[45]|avc|hq|4k|8k|2k|"
    r"1080[pi]?|720[pi]?|2160[pi]?|1440[pi]?|576[pi]?|480[pi]?|"
    r"50fps|60fps|30fps|25fps|50p|60p|"
    r"高码率?|原画|超高清|高清|超清|蓝光|标清|流畅|极速"
    r")(?![a-z0-9])"
)

_SEPCHARS = " \u3000_-\u2014\u00b7\u2022|/\\()[]<>\"'`~!@#$%^&*+=,\u3001,\u3002:\uff1a;\uff1b?\uff1f\u300c\u300d"
_SPLIT = re.compile("[" + re.escape(_SEPCHARS) + "]+")

_BUILTIN_ALIASES = {
    "央视一套": "cctv1", "中央一台": "cctv1", "中央一套": "cctv1", "cctv1综合": "cctv1",
    "央视二套": "cctv2", "中央二台": "cctv2", "cctv2财经": "cctv2",
    "央视三套": "cctv3", "中央三台": "cctv3", "cctv3综艺": "cctv3",
    "央视四套": "cctv4", "中央四台": "cctv4", "cctv4中文国际": "cctv4",
    "央视五套": "cctv5", "中央五台": "cctv5", "cctv5体育": "cctv5",
    "央视五加": "cctv5+", "cctv5+体育赛事": "cctv5+", "cctv5plus": "cctv5+",
    "央视六套": "cctv6", "中央六台": "cctv6", "cctv6电影": "cctv6",
    "央视七套": "cctv7", "中央七台": "cctv7", "cctv7国防军事": "cctv7",
    "央视八套": "cctv8", "中央八台": "cctv8", "cctv8电视剧": "cctv8",
    "央视九套": "cctv9", "中央九台": "cctv9", "cctv9纪录": "cctv9",
    "央视十套": "cctv10", "中央十台": "cctv10", "cctv10科教": "cctv10",
    "央视十一套": "cctv11", "cctv11戏曲": "cctv11",
    "央视十二套": "cctv12", "cctv12社会与法": "cctv12",
    "央视十三套": "cctv13", "cctv13新闻": "cctv13",
    "央视十四套": "cctv14", "cctv14少儿": "cctv14",
    "央视十五套": "cctv15", "cctv15音乐": "cctv15",
    "央视十六套": "cctv16", "cctv16奥林匹克": "cctv16",
    "央视十七套": "cctv17", "cctv17农业农村": "cctv17",
}


def to_simplified(text: str) -> str:
    return "".join(_TRAD.get(ch, ch) for ch in text)


def normalize(text: str) -> str:
    """把频道名归一化成可比较的 key。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = to_simplified(s).lower()
    s = _NOISE.sub("", s)
    s = _SPLIT.sub("", s)
    return s.strip()


def display_name(text: str) -> str:
    """用于展示的清洗后名称（保留原样大小写，只去噪）。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = _NOISE.sub("", s)
    s = re.sub(r"[_\-—·•|]{2,}", " ", s)
    s = re.sub("[_\\-\u2014\u00b7\u2022]{2,}", " ", s)
    s = s.strip(" _-\u2014\u00b7\u2022|")
    return s


@dataclass(slots=True)
class Match:
    channel: Channel | None
    key: str = ""
    confidence: int = 0  # 3=精确 2=别名 1=包含 0=未匹配


class ChannelMatcher:
    """基于模板的频道匹配器。"""

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels
        self._exact: dict[str, Channel] = {}
        self._alias: dict[str, Channel] = {}
        for ch in channels:
            key = normalize(ch.name)
            if key:
                self._exact.setdefault(key, ch)
            for alias in ch.aliases:
                akey = normalize(alias)
                if akey:
                    self._alias.setdefault(akey, ch)
        for alias, target in _BUILTIN_ALIASES.items():
            ch = self._exact.get(normalize(target))
            if ch is not None:
                self._alias.setdefault(normalize(alias), ch)

        # 包含匹配用的候选 key（长的优先）
        self._keys: list[tuple[str, Channel]] = sorted(
            ((k, v) for k, v in self._exact.items()), key=lambda kv: -len(kv[0])
        )

    def match(self, raw_name: str) -> Match:
        key = normalize(raw_name)
        if not key:
            return Match(None, "", 0)
        if key in self._exact:
            return Match(self._exact[key], key, 3)
        if key in self._alias:
            return Match(self._alias[key], key, 2)
        for candidate, channel in self._keys:
            if len(candidate) < 2:
                continue
            idx = key.find(candidate)
            if idx < 0:
                continue
            # 防止 CCTV1 命中 CCTV11：数字结尾要做边界检查
            tail = key[idx + len(candidate): idx + len(candidate) + 1]
            if candidate[-1].isdigit() and tail.isdigit():
                continue
            return Match(channel, key, 1)
        return Match(None, key, 0)


def parse_channels(text: str) -> list[Channel]:
    """解析频道模板：`分组,#genre#` 分段，每行 `频道名[,别名1,别名2...]`。"""
    channels: list[Channel] = []
    group = "未分组"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#genre#" in line:
            group = line.split(",", 1)[0].strip() or group
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if not parts:
            continue
        channels.append(Channel(name=parts[0], group=group, aliases=parts[1:]))
    return channels


def load_channels(path: Path) -> list[Channel]:
    if not path.exists():
        return []
    return parse_channels(path.read_text(encoding="utf-8", errors="ignore"))
