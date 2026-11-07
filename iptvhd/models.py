"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


# 各档画质「能连续播」所需的带宽（Mbps）。1080P 直播码率普遍 4~8 Mbps，
# 720P 在 2~4 Mbps；低于这个数播放器就会一直等下一片，表现就是卡一下停一下。
_NEED_MBPS = ((2160, 8.0), (1080, 4.0), (720, 2.0))


def need_mbps(height: int) -> float:
    """这一档画质大概需要多少带宽才播得动。"""
    for h, mbps in _NEED_MBPS:
        if height >= h:
            return mbps
    return 1.0


def _prefer_rank(height: int) -> int:
    """同档速度下的画质偏好：1080P > 720P > 4K > 其他。

    4K 排在 1080P 后面不是嫌它差，而是它的码率是 1080P 的三四倍，电视盒子和
    网页播放器更容易解不动、更容易卡。只有同台 1080P 全都不行时，才退到 4K
    顶上去（官方源经常只有 4K 一路，湖南卫视就是这种情况）。
    """
    if 1080 <= height < 2160:
        return 0
    if 720 <= height < 1080:
        return 1
    if height >= 2160:
        return 2
    return 3


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
    alt_name: str = ""          # 备用频道名（raw_name 匹配不上时再试）
    group: str = ""             # 采集到的原始分组
    tvg_id: str = ""
    logo: str = ""
    source: str = ""            # 来自哪个采集器
    slug: str = ""              # 上游频道标识，用于本机转发地址
    user_agent: str = ""        # 该条流需要指定的 UA（可选）
    hint_height: int = 0        # 采集端已知的分辨率（媒体清单里读不到时用）
    first_segment: str = ""     # 采集端已验证过的首个分片地址（省一次列表请求）
    segment_duration: float = 0.0  # 分片时长（秒），用来从分片大小推算码率
    playlist_only: bool = False  # 采集端能保证列表有效：分片被限流时不做否决
    verified_at: float = 0.0    # 采集端确认「清单 + 分片」可用的时刻（time.time()）
    stale: bool = False         # 本轮没取到，沿用上一轮的地址
    note: str = ""              # 补充说明，不参与过滤

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

    # ---- 滚动校验（由探测的第二阶段用，不参与过滤）----
    roll_url: str = ""          # 需要复核「清单是否在滚」的清单地址，空 = 不用复核
    roll_seq: int = -1          # 首次取到的分片序号

    # ---- 匹配结果（由管线回填）----
    channel: str = ""
    channel_group: str = ""

    @property
    def grade(self) -> str:
        return height_grade(self.height)

    def sort_key(self) -> tuple:
        """排序：直连优先 → 速度能不能撑住画质 → 画质 → 速率 → 延迟。

        第一项区分「能不能直连」：有真实地址的静态源排前面，只登记了 slug、
        地址要等播放时现换的源（央视频接口）排后面。换票本身会吃上游的按 IP
        配额，能不用就不用。

        第二项是这次补的：**只看画质不看速度会选出必卡的源**。湖南卫视的
        1080P 源实测 1.33 Mbps，1080P 直播至少要 4 Mbps，播起来只能一直等分片；
        而它一直排第一，仅仅因为分辨率标签比 720P 那条高。
        """
        direct = 0 if self.url else 1
        need = need_mbps(self.height)
        if self.speed_mbps <= 0:
            fit = 1          # 没测到速率：放在「够快」之后、「不够快」之前
        elif self.speed_mbps >= need:
            fit = 0          # 撑得住这一档画质
        else:
            fit = 2          # 标着高画质却跑不动，宁可用低一档的
        return (direct, fit, _prefer_rank(self.height), -self.speed_mbps, self.delay_ms)


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
    # 模板里有、这轮却拿不到的台；reason 说明是上游没有还是被限流
    missing: list[dict] = field(default_factory=list)

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
            "missing": self.missing,
        }
