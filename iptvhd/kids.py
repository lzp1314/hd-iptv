"""少儿频道派生视图。

少儿台散落在央视频（CCTV-14）、咪咕（少儿分类）、第三方聚合源（各省少儿台
+ 动画轮播）里。本模块提供频道名识别规则：给独立采集器 ``KidsCollector`` 从
这三路各取所需，输出成一个真正的「少儿频道」模块分组，和其它启用的模块一起
出现在**同一条**订阅地址里；不再单独给家长一条 ``/kids.m3u``。

只把「频道名 → 少儿 / 动画」这张规则放在这里，采集逻辑在 generators.py，
输出过滤在 service.py——三处共用一套判断，不会出现面板关了动画、订阅里还在。

分两组，方便家长单独关掉内容不可控的动画轮播：

* ``official`` —— 官方少儿台（央视/各省少儿/卡通卫视），内容可控、适龄有保证；
* ``cartoon`` —— 第三方动画轮播（海绵宝宝、猫和老鼠等），台多但播什么由上游
  决定、适龄性我们管不了，默认包含但可在面板单独关闭。
"""

from __future__ import annotations

from .models import Channel

# 专属订阅里显示的分组名（只改 M3U group-title，不改 Channel.group——
# 线路表按真实 group 建索引，改了会让 /live/c:... 找不到线路）。
KIDS_MODULE = "少儿频道"

# 官方少儿台：规范化后的子串匹配。覆盖央视少儿、各省少儿、卡通卫视、动漫频道。
# 故意不用宽泛的「动画」——聚合源有个「动画频道」组全是第三方轮播，
# 用「动画」会把它们误收进官方组。
_OFFICIAL_WORDS = (
    "CCTV14",       # CCTV-14（央视频/聚合源里常不带「少儿」二字）、CCTV14少儿
    "少儿",         # 各省少儿台、卡酷少儿…
    "儿童",         # 儿童频道
    "青少",         # 辽宁教育青少
    "卡酷",         # 卡酷少儿、卡酷动画（北京少儿卫视）
    "卡通",         # 金鹰/优漫/嘉佳/炫动/CN 卡通
    "动漫",         # 动漫秀场、新动漫、爱动漫（咪咕/百视通官方动画台）
    "动画大集合",   # 经典动画大集合（咪咕少儿分类）
)
# 个别不含上述关键词的官方台上这里（目前留空，按需补）。
_OFFICIAL_EXACT: frozenset[str] = frozenset()

# 第三方动画轮播：精确名，避免误伤。这些内容的适龄性由上游决定。
_CARTOON_EXACT: frozenset[str] = frozenset({
    "海绵宝宝", "猫和老鼠", "火影忍者", "蜡笔小新",
    "七龙珠", "中华小当家", "漫画解说",
})


def _norm(name: str) -> str:
    """频道名归一：去空格/连字符/间隔号并转大写，让 CCTV-14 与 CCTV14 对齐。"""
    return "".join(
        ch for ch in str(name).upper()
        if ch not in " \t-—_·.　"
    )


def classify(name: str) -> str:
    """频道名 → ``official`` / ``cartoon`` / ``""``（非少儿）。"""
    n = _norm(name)
    if n in {_norm(x) for x in _CARTOON_EXACT}:
        return "cartoon"
    if n in {_norm(x) for x in _OFFICIAL_EXACT} or any(w in n for w in _OFFICIAL_WORDS):
        return "official"
    return ""


def kids_channels(
    channels: list[Channel], *, include_cartoons: bool = True
) -> list[Channel]:
    """从完整频道表里派生少儿列表。

    返回的是**原 Channel 对象**（不改 group/streams/slug）——线路表按 ``group``
    建索引，改写会让播放地址失效。传入的列表应当已经过隐藏 / 模块开关过滤。
    """
    out: list[Channel] = []
    for ch in channels:
        kind = classify(ch.name)
        if kind == "official" or (include_cartoons and kind == "cartoon"):
            out.append(ch)
    return out
