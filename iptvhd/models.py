"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


def height_grade(height: int) -> str:
    """按高度给出画质等级标签。"""
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K"
    if height >= 1080:
        return "1080P"
    if height >= 720:
        return "720P"
    if height > 0:
        return "SD"
    return "未知"


@dataclass(slots=True)
class Stream:
    """一条候选直播流。"""

    url: str
    raw_name: str = ""          # 采集到的原始频道名
    group: str = ""             # 采集到的原始分组
    tvg_id: str = ""
    logo: str = ""
    source: str = ""            # 来自哪个采集器
    user_agent: str = ""        # 该条流需要指定的 UA（可选）

    # ---- 探测结果 ----
    ok: bool = False
    status: int = 0
    error: str = ""
    width: int = 0
    height: int = 0
    bandwidth: int = 0          # bps
    codec: str = ""
    delay_ms: int = 0           # 首字节延迟
    speed_mbps: float = 0.0     # 实测下载速率（Mbps）

    # ---- 匹配结果（由管线回填）----
    channel: str = ""
    channel_group: str = ""

    @property
    def grade(self) -> str:
        return height_grade(self.height)

    def sort_key(self) -> tuple:
        """排序：分辨率优先，其次速率，再次延迟。"""
        return (-self.height, -self.speed_mbps, self.delay_ms)


@dataclass(slots=True)
class Channel:
    """模板中定义的频道。"""

    name: str
    group: str = "未分组"
    aliases: list[str] = field(default_factory=list)
    streams: list[Stream] = field(default_factory=list)

    def accept(self, max_streams: int) -> None:
        """按质量裁剪保留的源数量，并去重。"""
        seen: set[str] = set()
        ordered: list[Stream] = []
        for s in sorted(self.streams, key=lambda x: x.sort_key()):
            if s.url in seen:
                continue
            seen.add(s.url)
            ordered.append(s)
            if len(ordered) >= max_streams:
                break
        self.streams = ordered


@dataclass(slots=True)
class RunReport:
    """一次更新的统计。"""

    started_at: str = ""
    finished_at: str = ""
    elapsed: float = 0.0
    collected: int = 0
    probed: int = 0
    alive: int = 0
    published: int = 0
    channels_total: int = 0
    channels_matched: int = 0
    sources: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(self.elapsed, 2),
            "collected": self.collected,
            "probed": self.probed,
            "alive": self.alive,
            "published": self.published,
            "channels_total": self.channels_total,
            "channels_matched": self.channels_matched,
            "sources": self.sources,
        }
