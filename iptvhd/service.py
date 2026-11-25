"""内置 HTTP 服务：结果分发 + 网页面板 + 定时更新。"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__, writers
from .epg import Epg
from .logos import Logos
from .models import Channel, Stream
from .pipeline import load_result, load_sources, run_update
from .relay import (
    Relay, RelayError, ScanGuard, channel_name, channel_slug, is_vlc_ua,
    pad_body, padded_playlist, static_ua,
)
from .region import detect
from .settings import Settings, clean_secret, coerce_panel, display_module, write_panel

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

M3U_TYPE = "audio/x-mpegurl; charset=utf-8"

# 反代把 Host 改回内网地址时，服务端就再也没法从请求里看出外网域名了。
# 所以只要见过一次外网地址（面板、订阅、播放都算），就记下来，
# 之后经反代进来的请求生成播放地址时用它，免得订阅列表里全是 192.168.x.x。
_SEEN_PUBLIC: dict[str, object] = {"base": "", "at": 0.0}
_SEEN_TTL = 7 * 86400.0


def _looks_local(host: str) -> bool:
    """判断 Host 是不是只能在局域网里用的地址。"""
    name = host.strip().lower()
    if not name or name.startswith("["):        # IPv6 字面量一律当本地
        return True
    name = name.rsplit(":", 1)[0] if ":" in name else name
    if name in ("localhost", "0.0.0.0"):
        return True
    if name.startswith(("127.", "10.", "192.168.", "169.254.")):
        return True
    if name.startswith("172."):
        try:
            return 16 <= int(name.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return name.endswith((".local", ".lan", ".home"))


def _seen_public() -> str:
    base = str(_SEEN_PUBLIC.get("base") or "")
    at = float(_SEEN_PUBLIC.get("at") or 0.0)
    if base and time.time() - at <= _SEEN_TTL:
        return base
    return ""


def _public_base(headers, public_url: str = "") -> str:
    """算出这次请求该用哪个对外地址。

    配了 ``PUBLIC_URL`` 就用它；否则按请求头推。套了反向代理时，
    ``X-Forwarded-Host`` / ``Forwarded`` 会带出真实的外网域名和端口。

    有些反代图省事，把 ``Host`` 改回内网地址又不传 forwarded 头，这时候服务端
    看到的就是 ``192.168.x.x``。遇到这种情况退回「以前见过的那个外网地址」——
    总比给外网播放器一个内网 IP 强。内网直连（没有 forwarded 头）不这样处理，
    否则内网看个电视还要绕到外网域名去。
    """
    if public_url:
        return public_url.rstrip("/")

    host = (headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    if not host:
        # RFC 7239：Forwarded: for=1.2.3.4;host=tv.example.com;proto=https
        for part in (headers.get("Forwarded") or "").split(";"):
            key, _, value = part.partition("=")
            if key.strip().lower() == "host":
                host = value.strip().strip('"')
                break
    if not host:
        host = (headers.get("X-Original-Host") or "").split(",")[0].strip()
    if not host:
        host = (headers.get("Host") or "").strip()
    if not host:
        return _seen_public()

    proto = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "https" if headers.get("X-Forwarded-Ssl") == "on" else "http"
    here = f"{proto}://{host}"

    if not _looks_local(host):
        _SEEN_PUBLIC["base"] = here
        _SEEN_PUBLIC["at"] = time.time()
        return here

    via_proxy = any(headers.get(h) for h in ("X-Forwarded-For", "X-Real-IP", "X-Forwarded-Host"))
    if via_proxy:
        return _seen_public() or here
    return here


def secret_prefix(secret: str) -> str:
    """密码段的 URL 形式：``/abc``；没设密码就是空串。"""
    return f"/{quote(secret, safe='')}" if secret else ""


def with_secret(base: str, secret: str) -> str:
    """把密码段接到对外地址后面。

    订阅、台标、节目单、转发地址全都从这一个 base 派生，所以只要这里带上密码，
    整条链路（清单 → 分片 → 台标）就都带着，不用逐处改。
    用户如果自己把密码写进 ``PUBLIC_URL`` 了，就别重复接一次。
    """
    if not base or not secret:
        return base
    base = base.rstrip("/")
    for tail in (secret_prefix(secret), "/" + secret):
        if base.endswith(tail):
            return base
    return base + secret_prefix(secret)


def _channels_from_result(result: dict) -> list[Channel]:
    """把 result.json 还原成 Channel/Stream，供按请求现拼 m3u。

    地址、画质这些落过盘的信息都在，唯独没有 logo 和 UA——这两个由
    服务端自己补（台标走 /logo，UA 走中转），不需要从文件里读。
    """
    channels: list[Channel] = []
    for row in result.get("channels", []) or []:
        streams = []
        for item in row.get("streams", []) or []:
            url = str(item.get("url") or "")
            slug = str(item.get("slug") or "")
            # 采集阶段可以只登记 slug、不取地址（地址在播放时现换）
            if not url and not slug:
                continue
            streams.append(
                Stream(
                    url=url,
                    slug=slug,
                    height=int(item.get("height") or 0),
                    width=int(item.get("width") or 0),
                    # 编码要带上：面板据此避开 H.265（浏览器解不了）
                    codec=str(item.get("codec") or ""),
                )
            )
        name = str(row.get("name") or "")
        if name and streams:
            channels.append(Channel(name=name, group=str(row.get("group") or "未分组"), streams=streams))
    return channels


def _visible(channels: list[Channel], hidden: list[str]) -> list[Channel]:
    """去掉面板里隐藏掉的台。

    只过滤对外输出，result.json 里的原始条目一个不删——所以「恢复」是即时的，
    不用等下一轮更新把台重新采集回来。
    """
    if not hidden:
        return channels
    blocked = set(hidden)
    return [c for c in channels if c.name not in blocked]


def _module_channels(result: dict | None) -> dict[str, dict]:
    """从最近一次结果里按展示模块汇总「抓到了哪些频道」。

    只做本地汇总、不发网络。返回 ``{模块名: {"total": N, "groups": {分类: [频道名]}}}``，
    分类用央视 / 卫视 / 其它三档，供面板模块卡片顶部显示。result 可能是旧文件或缺
    字段，一律按空处理，绝不让面板因为一份坏结果打不开。
    """
    if not result:
        return {}
    channels = result.get("channels")
    if not isinstance(channels, list):
        return {}
    out: dict[str, dict] = {}
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        module = display_module(str(ch.get("group") or "").strip()) or "未分组"
        name = str(ch.get("name") or "").strip()
        if not name:
            continue
        bucket = out.setdefault(module, {"total": 0, "groups": {}})
        bucket["total"] += 1
        upper = name.upper()
        if "央视" in name or "CCTV" in upper or "CGTN" in upper or "CETV" in upper:
            cat = "央视"
        elif "卫视" in name:
            cat = "卫视"
        else:
            cat = "其它"
        bucket["groups"].setdefault(cat, []).append(name)
    for bucket in out.values():
        for names in bucket["groups"].values():
            names.sort()
    return out


def _module_catalog(settings: Settings, result: dict | None = None) -> dict:
    """面板「数据源」页的模块清单：内置大类 + 更多源。

    内置大类（咪咕视频 / 央视频 / 第三方聚合源 / 运营商官方源）置顶，点开可展开
    各自的配置；sources.toml 里其余源收进「更多源」折叠区，仍各自一个开关。
    复用 load_sources：它已经把 sources.toml、注入的咪咕、本省自动源汇到一起，
    并把面板关掉的标成 enabled=False。这里只读本地配置，不发网络。
    ``result`` 传入时，每个模块额外带上最近一次抓到的频道（分类 + 频道名）。
    """
    from .settings import BUILTIN_MODULES, display_module

    try:
        sources = load_sources(settings)
    except Exception:  # noqa: BLE001 - 面板不该因为一份写坏的 sources.toml 打不开
        sources = []

    by_name = {
        str(x.get("name") or "").strip(): x
        for x in sources
        if str(x.get("name") or "").strip()
    }
    found = _module_channels(result)
    # 采集统计：底层源名 → 抓取条数。模块关掉时 result 里可能没有它的频道，
    # 这时用采集条数兜底，卡片上仍能显示「上次抓到 N 条」。
    stats = {
        str(row.get("name") or "").strip(): row
        for row in (((result or {}).get("report", {}) or {}).get("sources", []) or [])
        if str(row.get("name") or "").strip()
    }
    builtins: list[dict] = []
    for module, members in BUILTIN_MODULES.items():
        # 模块只要有任意一个底层源在、且没被面板关，就算开着
        present = [m for m in members if m in by_name]
        enabled = any(bool(by_name[m].get("enabled", True)) for m in present)
        ch = found.get(module, {"total": 0, "groups": {}})
        collected = sum(int(stats.get(m, {}).get("count") or 0) for m in members)
        builtins.append({
            "name": module,
            "members": list(members),
            "types": sorted({str(by_name[m].get("type") or "") for m in present if by_name[m].get("type")}),
            "enabled": enabled,
            "configurable": module in {"咪咕视频", "央视频"},
            "channels": ch["groups"],
            "count": ch["total"],
            "collected": collected,
        })

    from .kids import KIDS_MODULE

    # 少儿频道有专属卡片和独立开关，不在 BUILTIN_MODULES 里；这里也把它的
    # 频道清单带出去，卡片和配置弹窗才能像其它模块一样列出「抓到了哪些台」。
    kids_found = found.get(KIDS_MODULE, {"total": 0, "groups": {}})
    kids_stats = stats.get(KIDS_MODULE) or {}
    kids = {
        "name": KIDS_MODULE,
        "channels": kids_found["groups"],
        "count": kids_found["total"],
        "collected": int(kids_stats.get("count") or 0) or kids_found["total"],
    }

    # 少儿是独立采集模块，但它有专属卡片和独立开关，不能再混进「更多源」
    covered = {m for members in BUILTIN_MODULES.values() for m in members} | {KIDS_MODULE}
    others: list[dict] = []
    for name, item in by_name.items():
        if name in covered:
            continue
        others.append({
            "name": name,
            "type": str(item.get("type") or ""),
            "enabled": bool(item.get("enabled", True)),
        })
    return {"builtins": builtins, "others": others, "kids": kids}


def _module_disabled_names(settings: Settings) -> set[str]:
    """把关闭的模块开关归一成展示模块名，供订阅和「我的频道」共用一套判断。

    旧配置里关的可能是底层源名或展示名，统一经 display_module 归一，升级后
    原先关掉的模块仍然关着。两个出口都读这里，避免规则漂移——以前订阅过滤
    了、「我的频道」没过滤，才会出现"关掉的源还在列表里"的不一致。
    """
    from .kids import KIDS_MODULE

    names = {display_module(x) for x in settings.disabled_modules}
    # 少儿有自己的 kids_enabled 开关，不在 disabled_modules 里；没开也按
    # 「模块关掉」处理，主订阅和「我的频道」才不会出现一个空壳少儿分组。
    if not settings.kids_enabled:
        names.add(KIDS_MODULE)
    return names


def _module_visible(channels: list[Channel], settings: Settings) -> list[Channel]:
    """订阅输出按启用的模块过滤：关掉的分组立刻消失，不用重新采集。"""
    disabled = _module_disabled_names(settings)
    if not disabled:
        return channels
    return [c for c in channels if display_module(c.group) not in disabled]


def _kids_visible(channels: list[Channel], settings: Settings) -> list[Channel]:
    """动画轮播子开关：只在少儿分组里剔台，不改其它模块的内容。

    子开关走输出层过滤，和隐藏台一样点完保存就生效，不用等下一轮采集。
    """
    if settings.kids_cartoons:
        return channels
    from .kids import KIDS_MODULE, classify

    return [
        c for c in channels
        if display_module(c.group) != KIDS_MODULE or classify(c.name) != "cartoon"
    ]


def _row_module_visible(row: dict, disabled: set[str]) -> bool:
    """「我的频道」的 result.json 行按模块过滤，规则与订阅 _module_visible 一致。

    result.json 里每行是 dict（group 为展示模块名），不是 Channel 对象，所以单独
    走这一层，但判断逻辑一样：group 归一后不在关闭集合里就留下。
    """
    if not disabled:
        return True
    return display_module(str(row.get("group") or "")) not in disabled


def _row_kids_visible(row: dict, settings: Settings) -> bool:
    """「我的频道」行级少儿动画过滤，与订阅的 _kids_visible 同规则。"""
    if settings.kids_cartoons:
        return True
    from .kids import KIDS_MODULE, classify

    return (
        display_module(str(row.get("group") or "")) != KIDS_MODULE
        or classify(str(row.get("name") or "")) != "cartoon"
    )


def _kids_only(channels: list[Channel], settings: Settings) -> list[Channel]:
    """从已过滤的频道表里派生少儿订阅（输出层视图，不落盘、不改原始数据）。

    两组：官方少儿台 + 第三方动画轮播（后者可在面板单独关）。返回的仍是原
    Channel 对象——group 保持真实值，线路表按它建索引，改了会播不了。
    """
    from .kids import kids_channels

    return kids_channels(channels, include_cartoons=settings.kids_cartoons)


def _ysp_resolver(slug: str, ip_mode: str = "auto") -> tuple[str, str]:
    """中转层拿不到频道表之外的地址，这里的解析器只认 ysp 里的台。

    ``ip_mode`` 是央视频模块的地址族三态：auto/ipv4/ipv6。
    """
    from . import ysp

    row = ysp.CHANNEL_BY_SLUG.get(slug.lower())
    if row is None:
        raise RelayError(f"未知频道: {slug}")
    url, _, _ = ysp.resolve(row[2], row[3], row[4], attempts=2, ip_mode=ip_mode)
    return url, ysp.UA


def _ysp_cooling() -> bool:
    """央视频出口是否正处在上游限流冷却（8 次拒绝后停 30 秒）。

    只给 relay 在「手里没有可用地址」时看一眼；正在播的台仍用旧地址顶住。
    延迟导入，避免把网络层提前拉进服务模块。
    """
    from . import ysp

    return ysp.denied()


def migu_pid(slug: str) -> str:
    """``m:<pid>`` → pid。不是咪咕 slug 返回空串。"""
    return slug[2:] if slug.startswith("m:") and slug[2:].isdigit() else ""


def migu_ticket(slug: str, settings: Settings) -> str:
    """给咪咕频道换一张当前有效的调度地址。

    账号、画质、H.265 都只在这里参与换票，不写进地址、日志或订阅。
    返回的是**未请求过**的调度地址——交给播放器后，由平台按播放器的
    网络就近调度，这一步是外省流畅的关键（见 migu 模块说明）。
    """
    from . import migu
    from .net import HttpClient

    pid = migu_pid(slug)
    if not pid:
        raise RelayError(f"不是咪咕频道: {slug}")
    return migu.ticket(
        pid,
        settings.migu_rate,
        user_id=settings.migu_user_id,
        token=settings.migu_token,
        h265=settings.migu_h265,
        client=HttpClient(timeout=12, retries=1, ip_mode=settings.module_ip_mode("咪咕视频")),
    ).url


def migu_edge_url(slug: str, settings: Settings) -> str:
    """换票后请求调度地址读一次 302，返回真实节点地址（给播放器直连）。

    读不到可用节点时返回空串，调用方回落到 relay 代取。账号、画质只在这里
    参与换票，不写进返回的地址。
    """
    from . import migu
    from .net import HttpClient

    client = HttpClient(timeout=6, retries=1, ip_mode=settings.module_ip_mode("咪咕视频"))
    return migu.edge_location(migu_ticket(slug, settings), client=client)


def live_target(raw_slug: str) -> tuple[str, str] | None:
    """判断 ``/live/<slug>.m3u8`` 里的 slug 该交给谁，返回 ``(key, 展示名)``。

    三套 slug 并存：

    - ``cctv1`` 这类：央视频的台，要现换票，地址不在 URL 里；
    - ``o:<base64>`` 这类：官方源（运营商 IPTV 等），上游地址整条编在 slug 里；
    - ``c:<base64>`` 这类：**频道级**，一个地址对应这个台的几条线路，具体走哪条
      由中转层现场决定（见 ``State._resolve``）。

    只认前者的话，官方源接进来之后所有台都会回 404「未知频道」，播放器那边
    看到的就是「源服务器返回错误」——0.9.13 就是这么翻车的。播放器还会把
    ``o:`` 编码成 ``o%3A``，所以进来先还原。
    """
    from . import ysp
    from .relay import static_url

    key = unquote(raw_slug)
    if migu_pid(key):
        # 咪咕频道按 pID 现换票，名字从频道表拿（拿不到就用编号，不影响播放）
        from . import migu

        label = migu.name_of(key[2:]) or f"咪咕-{key[2:]}"
        return key, label
    row = ysp.CHANNEL_BY_SLUG.get(key.lower())
    if row is not None:
        return key.lower(), row[1]
    name = channel_name(key)
    if name:
        return key, name
    if static_url(key):
        return key, ("运营商源" if key.startswith("o:") else "上游源")
    return None


class State:
    """进程内共享状态。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.running = False
        self.last_started = ""
        self.last_finished = ""
        self.last_error = ""
        self.report: dict = {}
        self.logs: list[str] = []
        # ---- 频道级线路表 ----
        # slug（``c:...``）→ 这个台的几条线路，按质量排好序。线路本身不存
        # 在这里，而是每次 latest.json 变了就从里面重建：更新一轮之后哪条线路
        # 好哪条坏全变了，留着旧的等于拿上一轮的结论播这一轮。
        self._lines: dict[str, list[tuple[str, str, str]]] = {}
        self._lines_mtime = 0.0
        self._lines_lock = threading.Lock()
        # slug → 当前在用第几条。轮换是「失败驱动」的：只有上游真的拒了才往下
        # 走一条，正常播放不会自己漂移。
        self._line_at: dict[str, int] = {}
        self._line_lock = threading.Lock()
        # slug → 上次记「换线」日志的时刻，同一台 30 秒只记一条
        self._line_note: dict[str, float] = {}
        # 播放器「预览图/检测可用性」会把几十个台挨个点一遍。按客户端先挡一道，
        # 别让多设备各扫几个的流量汇聚起来把出口打进限流。
        self.scan_guard = ScanGuard()
        # 清单由本机取（用上游认的 UA），分片默认让播放器直连 CDN
        self.relay = Relay(
            self._resolve,
            prefetch_every=settings.playlist_ttl,
            cache_bytes=int(settings.relay_cache_mb * 1024 * 1024),
            mode=settings.play_mode,
            auto_switch=settings.auto_switch,
            upstream_denied=_ysp_cooling,
            plain_http=settings.ysp_plain,
        )
        self.epg = Epg(settings.data_dir, settings.epg_source, settings.epg_ttl_hours)
        self.logos = Logos(settings.data_dir / "logos")
        # 这条宽带是哪里的：只用于面板展示和你自己判断该开哪些源，
        # 采集流程一概不看它 —— 识别失败也不会少一个台。
        self.region: dict = {}
        self._region_lock = threading.Lock()
        self._region_busy = False
        # 「清单冻住」这类事只在这里看得见，接到面板的运行日志里
        self.relay.attach_log(self.log)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-200:]

    # ---------------- 频道级线路 ----------------
    def _ensure_lines(self) -> None:
        """按 ``latest.json`` 重建线路表；文件没变就什么也不做。

        为什么不常驻内存一次性建好：每轮更新之后「哪条线路好」全变了，而
        latest.json 是唯一写这份结论的地方。用 mtime 判断，比在每个更新出口
        都去通知一遍可靠——手动跑一次 CLI、换个容器重建，这里都能跟上。
        """
        path = self.settings.data_dir / "latest.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == self._lines_mtime:
            return
        with self._lines_lock:
            if mtime == self._lines_mtime:
                return
            lines: dict[str, list[tuple[str, str]]] = {}
            result = load_result(self.settings) or {}
            for row in result.get("channels") or []:
                name = str(row.get("name") or "")
                if not name:
                    continue
                items: list[tuple[str, str]] = []
                for item in row.get("streams") or []:
                    url = str(item.get("url") or "").strip()
                    slug = str(item.get("slug") or "").strip()
                    if not url and not slug:
                        continue
                    items.append((url, slug))
                if items:
                    # 顺序就是更新时算好的质量排序，第一条最该用
                    group = str(row.get("group") or "")
                    lines[channel_slug(name, group)] = items
            self._lines = lines
            self._lines_mtime = mtime
            self._line_at.clear()

    def _resolve(self, slug: str, rotate: bool = False) -> tuple[str, str]:
        """给中转层解析一条线路。

        频道级 slug（``c:``）：这个台挂着几条源，按顺序取当前这条；``rotate``
        表示刚被上游拒了，换下一条。换到头就回到第一条——比「所有线路都试过
        就报错」好：上游的 403 大多是按 IP、按分钟计的，绕一圈回来往往就好了。

        其它 slug（央视频的 ``cctv1``）：交给原来的解析器，它要现换票。
        """
        # 裸咪咕 slug（订阅里只有一条线路、没法做频道级 failover 时写的就是
        # /live/m:<pid>）：没有 c: 线路表可查，直接按 pID 现换票交给 relay 取清单。
        # 不处理的话会掉进下面的 _ysp_resolver，咪咕台全部解析失败。
        if migu_pid(slug):
            return migu_ticket(slug, self.settings), self.settings.user_agent

        name = channel_name(slug)
        if not name:
            return _ysp_resolver(slug, ip_mode=self.settings.module_ip_mode("央视频"))

        self._ensure_lines()
        items = self._lines.get(slug) or []
        if not items:
            raise RelayError(f"{name} 暂时没有可用线路（结果可能已过期，等下一轮更新）")

        with self._line_lock:
            if rotate and len(items) > 1:
                self._line_at[slug] = (self._line_at.get(slug, 0) + 1) % len(items)
            idx = self._line_at.get(slug, 0) % len(items)
            moved = rotate and len(items) > 1
            now = time.time()
            # 同一台 30 秒只记一条：上游按 IP 限流时，几十条请求会连着换线，
            # 每条都写日志等于把面板刷满，真正要看的错误反而被冲走
            if moved and now - self._line_note.get(slug, 0.0) >= 30.0:
                self._line_note[slug] = now
                quiet = False
            else:
                quiet = True
        if moved and not quiet:
            self.log(f"[换线] {name} 改用第 {idx + 1}/{len(items)} 条线路")

        url, inner = items[idx]
        if url:
            # 官方源（运营商的 IPv6 边缘）认机顶盒同款 UA；聚合源用采集时那个
            # 浏览器 UA —— 采集阶段就是用它取到清单的，说明上游认。
            ua = static_ua() if inner.startswith("o:") else self.settings.user_agent
            return url, ua
        if migu_pid(inner):
            # 走 relay 取清单时用采集验证过的浏览器 UA。咪咕最终节点清单对 UA
            # 挑剔，空 UA 会落到默认 qqlive、取不到——这里必须显式带浏览器 UA。
            return migu_ticket(inner, self.settings), self.settings.user_agent
        return _ysp_resolver(inner, ip_mode=self.settings.module_ip_mode("央视频"))

    def primary_migu_pid(self, slug: str) -> str:
        """这个台的首选线路是不是咪咕，是就返回它的 pID。

        只在咪咕排在第一条时才让播放器就近取流。咪咕排后面当备用时返回空，
        交给中转层按正常顺序处理——那种情况下它只是兜底，不该抢首选的位置。
        """
        # 直接访问 /live/m:<pid>.m3u8 的，本身就是咪咕
        if migu_pid(slug):
            return migu_pid(slug)
        name = channel_name(slug)
        if not name:
            return ""
        self._ensure_lines()
        items = self._lines.get(slug) or []
        if not items:
            return ""
        _url, inner = items[0]
        return migu_pid(inner)

    # ---------------- 这条宽带是哪里的 ----------------
    def refresh_region(self, force: bool = False) -> dict:
        """识别省份 / 运营商 / IPv6，结果缓存在 output/region.json。

        放在后台线程跑：两次外部接口 + IPv6 探测最坏要七八秒，不能卡住启动。
        识别不出来也无所谓，面板上少一行字而已，采集和播放完全不受影响。
        """
        with self._region_lock:
            if self._region_busy:
                return self.region
            self._region_busy = True
        try:
            info = detect(
                self.settings.data_dir,
                manual_province=self.settings.region_hint,
                manual_isp=self.settings.isp_hint,
                force=force,
            )
        except Exception as exc:                     # noqa: BLE001 - 识别失败不算错误
            info = {"ok": False, "note": "没能识别出运营商（不影响使用）"}
            self.log(f"[网络] 识别宽带信息失败：{type(exc).__name__}")
        with self._region_lock:
            self._region_busy = False
            self.region = info
        if info.get("ok"):
            self.log(f"[网络] 检测到 {info.get('note')}")
        return info

    # ---------------- 设置 ----------------
    def current_settings(self, result: dict | None = None) -> dict:
        s = self.settings
        return {
            "interval_hours": s.interval_hours,
            "playlist_ttl": s.playlist_ttl,
            "cache_mb": int(s.relay_cache_mb),
            "play_mode": s.play_mode,
            # 央视频分片直连降 http（老旧电视盒子 TLS 兼容），面板可开关
            "ysp_plain": s.ysp_plain,
            "auto_region": s.auto_region,
            "region": s.region_hint,
            "isp": s.isp_hint,
            "auto_switch": s.auto_switch,
            "public_url": s.public_url,
            # 只读：面板据此在每个地址后面带上密码段，前端不允许写回
            "secret": s.secret,
            "epg_url": s.epg_url,
            "epg_source": s.epg_source,
            "hidden": s.hidden,
            # 「数据源」页：所有源模块 + 面板关掉的名字（只读，改走 btnModuleSave）
            "modules": _module_catalog(s, result),
            "disabled_modules": s.disabled_modules,
            # 每个模块的地址族三态（auto/ipv4/ipv6），给面板弹窗回显
            "module_ip_mode": s.module_ip_modes(),
            "migu_enabled": s.migu_enabled,
            # 账号非零即可：令牌本身绝不回传给面板，防止被截图/代理日志带走。
            "migu_account_set": bool(s.migu_user_id and s.migu_token),
            "migu_user_id": s.migu_user_id,
            "migu_rate": s.migu_rate,
            "migu_h265": s.migu_h265,
            "migu_client_dispatch": s.migu_client_dispatch,
            "migu_edge_dispatch": s.migu_edge_dispatch,
            "migu_refresh": s.migu_refresh_minutes,
            # 少儿频道模块的开关（主开关 + 动画轮播子开关）
            "kids_enabled": s.kids_enabled,
            "kids_cartoons": s.kids_cartoons,
        }

    def set_hidden(self, names: list[str], hidden: bool) -> list[str]:
        """隐藏 / 恢复若干频道，立刻落盘。

        对外的 m3u / txt 是每次请求现拼的，所以改完播放器重新拉一次列表就生效，
        不用重新采集（采集是零请求的，但也没必要为改个显示多跑一轮）。
        """
        from .settings import clean_names

        current = set(self.settings.hidden)
        for name in clean_names(names):
            if hidden:
                current.add(name)
            else:
                current.discard(name)
        value = sorted(current)
        write_panel(self.settings.panel_path, {"hidden": value})
        self.settings.panel["hidden"] = value
        return value

    def apply_settings(self, values: dict) -> dict:
        """面板提交的设置：落盘 + 当场生效，不用重启容器。

        密码是唯一会「当场把自己关在门外」的一项：改完之后旧地址立刻失效，
        所以校验放在这里，不合法就抛 ValueError（接口回 400），不做静默修正。
        """
        clean = coerce_panel(values)
        was_kids_on = bool(self.settings.kids_enabled)
        if "secret" in clean:
            clean["secret"] = clean_secret(clean["secret"])
        # 账号 ID 留空 = 清空账号，令牌也一起清掉（避免留下孤立的无效令牌）
        if "migu_user_id" in clean and not clean["migu_user_id"]:
            clean["migu_token"] = ""
        if clean:
            # module_ip_mode 是「每个模块一个键」的字典，面板一次只提交当前模块。
            # 直接覆盖会把别的模块已存的地址族清掉（它们会悄悄退回 auto），
            # 所以这里按模块合并，只改提交了的那一项。
            if "module_ip_mode" in clean:
                from .settings import merge_ip_modes

                clean["module_ip_mode"] = merge_ip_modes(
                    self.settings.panel.get("module_ip_mode"), clean["module_ip_mode"]
                )
            write_panel(self.settings.panel_path, clean)
            self.settings.panel.update(clean)
            if "kids_enabled" in clean and self.settings.kids_enabled and not was_kids_on:
                # 刚打开少儿：单独抓一轮，主订阅立刻就有少儿分组，
                # 不用等下一个定时全量周期；已有任务在跑就跳过，下一轮自然会带上。
                from .kids import KIDS_MODULE

                if self.trigger(KIDS_MODULE):
                    self.log("[少儿] 已开启，正在单独抓一轮少儿频道")
            self.relay.configure(
                prefetch_every=self.settings.playlist_ttl,
                cache_bytes=int(self.settings.relay_cache_mb * 1024 * 1024),
                mode=self.settings.play_mode,
                auto_switch=self.settings.auto_switch,
                plain_http=self.settings.ysp_plain,
            )
            if "epg_source" in clean:
                # 换了节目单源就把旧的清掉，免得播放器拿到对不上的节目单
                self.epg.source = self.settings.epg_source
                self.epg.clear()
                names = self._channel_names()
                if self.epg.source and names:
                    threading.Thread(target=self._warm_epg, args=(names,), daemon=True).start()
            if "region" in clean or "isp" in clean:
                # 改了省份/运营商：重新识别，并按新组合拉一次本省清单。
                # 放后台跑，别让面板保存卡在一个网络往返上。
                threading.Thread(target=self._apply_region_change, daemon=True).start()
        return self.current_settings()

    def _apply_region_change(self) -> None:
        """面板改了省/运营商：重新识别 + 按新组合拉一次本省清单。"""
        from .autoregion import refresh as refresh_auto

        try:
            self.refresh_region(force=True)
            refresh_auto(self.settings, log=self.log)
        except Exception as exc:                     # noqa: BLE001 - 面板操作不该抛到线程外
            self.log(f"[自动源] 按选定省份取源失败：{type(exc).__name__}")

    def _channel_names(self) -> list[str]:
        result = load_result(self.settings) or {}
        names = [str(c.get("name") or "") for c in result.get("channels", []) or []]
        return [n for n in names if n]

    def _warm_epg(self, names: list[str]) -> None:
        if not names:
            return
        try:
            mapping = self.epg.build(names)
            self.log(
                f"[节目单] 对上 {len(mapping)}/{len(names)} 个频道"
                + ("" if mapping else "（节目单源里没有这些台，或源不可达）")
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"! 节目单构建失败：{exc}")

    # ---------------- 更新 ----------------
    def trigger(self, module: str = "") -> bool:
        """异步触发一次更新，已有任务在跑时返回 False。

        ``module`` 非空时只重采这一个模块，其它模块沿用上一轮结果。
        """
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.last_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        threading.Thread(target=self._run, args=(module,), daemon=True).start()
        return True

    def _run(self, module: str = "") -> None:
        try:
            report = run_update(self.settings, log=self.log, only_module=module)
            with self.lock:
                self.report = report.as_dict()
                self.last_error = ""
        except Exception as exc:  # noqa: BLE001 - 服务不能因一次失败退出
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"! 更新失败：{self.last_error}")
        finally:
            with self.lock:
                self.running = False
                self.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        threading.Thread(target=self._warm_assets, daemon=True).start()

    def _warm_assets(self) -> None:
        """更新完顺手把节目单和台标备好，播放器来要时不用等。"""
        names = self._channel_names()
        if not names:
            return
        self._warm_epg(names)
        try:
            got = self.logos.prefetch(names)
            if got:
                self.log(f"[台标] 缓存 {got} 个")
        except Exception as exc:  # noqa: BLE001
            self.log(f"! 台标缓存失败：{exc}")


def make_handler(state: State):
    settings = state.settings

    class Handler(BaseHTTPRequestHandler):
        server_version = f"hd-iptv/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---------------- 基础工具 ----------------
        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            extra: dict | None = None,
            cache: str | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", cache or "no-cache")
            self.send_header("Accept-Ranges", "bytes")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # 播放器拖动进度或切台时会直接断开，不是错误，别刷栈
                    self.close_connection = True

        def _json(self, payload, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _file(self, path: Path, ctype: str, download: str = "", cache: str | None = None) -> None:
            if not path.exists():
                self._json({"error": "结果尚未生成，请先更新一次", "path": str(path)}, 404)
                return
            extra = {"Content-Disposition": f'inline; filename="{download}"'} if download else None
            self._send(200, path.read_bytes(), ctype, extra, cache)

        def _redirect(self, url: str) -> None:
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _base(self) -> str:
            """这次请求该用哪个对外地址。具体规则见模块里的 _public_base。"""
            return with_secret(_public_base(self.headers, settings.public_url), settings.secret)

        def _deny(self) -> None:
            """没带密码段时的回应。

            浏览器看到一页「身份认证失败」，播放器 / 脚本看到一段 JSON——两者都
            不透露密码本身，也不回 302 到别处（那等于告诉扫描器「这里有东西」）。
            """
            accept = (self.headers.get("Accept") or "").lower()
            if "text/html" in accept:
                page = (
                    "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                    "<title>身份认证失败</title>"
                    "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
                    "justify-content:center;background:#fff;color:#1a1a1a;"
                    "font:16px/1.8 -apple-system,'Segoe UI',sans-serif}"
                    "div{text-align:center;padding:24px}h1{font-size:20px;font-weight:500;margin:0 0 12px}"
                    "p{margin:4px 0;color:#666;font-size:14px}"
                    "code{background:#f3f4f6;padding:2px 6px;border-radius:4px;color:#333}</style>"
                    "<div><h1>身份认证失败</h1>"
                    "<p>这个服务设了访问密码，地址里必须带密码段。</p>"
                    "<p>正确格式：<code>http://主机:端口/密码/</code></p></div></html>"
                )
                self._send(401, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            self._json(
                {"error": "身份认证失败", "hint": "地址里需要带密码段：/<密码>/playlist.m3u"},
                401,
            )

        def _require(self) -> str | None:
            """校验访问密码，返回剥掉密码段之后的路由；不通过返回 None。

            设了密码就**全站**要求带：面板、订阅、清单、分片、台标、接口一个不漏。
            只保护订阅列表而不保护面板是没意义的——面板能改配置、能触发更新。
            """
            raw = urlsplit(self.path).path
            secret = settings.secret
            if not secret or raw == "/health":
                # /health 留给容器健康检查：它只回一句 ok，不含任何数据
                return raw
            for form in {secret, quote(secret, safe="")}:
                prefix = "/" + form
                if raw == prefix:
                    # 少了结尾的斜杠，页面里的相对路径会指到上一级去
                    self._redirect(prefix + "/")
                    return None
                if raw.startswith(prefix + "/"):
                    return raw[len(prefix) :] or "/"
            self._deny()
            return None

        # ---------------- 播放 ----------------
        def _client_key(self) -> str:
            """扫台防护的客户端标识：反代后的真实 IP + UA。"""
            fwd = (self.headers.get("X-Forwarded-For")
                   or self.headers.get("X-Real-IP") or "")
            ip = fwd.split(",")[0].strip() or self.client_address[0]
            ua = self.headers.get("User-Agent") or ""
            return f"{ip}|{ua}"

        def _live(self, slug: str) -> None:
            """返回改写后的播放清单。

            清单必须由本机取：上游只放行它自己的 UA，播放器拿自己的 UA 去要
            m3u8 会被回 403——这就是「列表里只有一个台能播」的老问题。
            取回来之后，清单里的分片地址默认换成上游绝对地址，让播放器**直连
            CDN**；经本机转发分片反而会被平台判成异常流量回 403（见 relay 模块
            开头的说明），所以这里不再代劳。
            """
            hit = live_target(slug)
            if hit is None:
                self._json({"error": f"未知频道: {slug}", "hint": "/api/live 可列出全部"}, 404)
                return
            key, name = hit

            # 播放器扫台先在本地挡住：10 秒内碰 6 个新频道就判定异常，
            # 之后 30 秒只允许这个客户端继续看它已经在看的台。
            # 一枪不打上游，出口额度留给真在播的人。
            allowed, retry = state.scan_guard.check(self._client_key(), key)
            if not allowed:
                body = json.dumps({
                    "error": "正在批量请求频道，已本地暂停新频道请求",
                    "retry_after": retry,
                    "hint": "关闭播放器的「检测可用性 / 预览图」后约 30 秒自动恢复；正在看的频道不受影响",
                }, ensure_ascii=False).encode("utf-8")
                self._send(429, body, "application/json; charset=utf-8",
                           {"Retry-After": str(retry)})
                return

            # 咪咕播放有三条路，优先级从高到低：
            #  1) edge_dispatch（默认）：服务端只请求调度域名读一次 302，把真实
            #     节点交给播放器直连，清单/分片走播放器网络。服务端不碰节点清单，
            #     所以 NAS 出口被节点回 661 时这条路仍能播——这是同机部署的可用路。
            #  2) client_dispatch（旧）：把未跳转的调度地址甩给播放器自己跳多层
            #     302，VLC 常常处理不好，保留给确认能用的场合。
            #  3) 都不开：走下面的 relay，本机取清单改写下发。
            # 任一路失败都回落到下一条，最后兜底 relay，不让单个源把频道整条搞没。
            # 只在首选线路就是咪咕时才走这些：咪咕当备用时仍走 relay 正常顺序。
            if settings.migu_enabled:
                top_pid = state.primary_migu_pid(key)
                if top_pid:
                    if settings.migu_edge_dispatch:
                        try:
                            node = migu_edge_url(f"m:{top_pid}", settings)
                            if node:
                                self._redirect(node)
                                return
                            state.log(f"[咪咕] {name} 调度地址没解析出节点，改用其它方式")
                        except Exception as exc:  # noqa: BLE001 - 换票失败回落其它线路
                            state.log(f"[咪咕] {name} 解析节点失败，改用其它方式：{exc}")
                    if settings.migu_client_dispatch:
                        try:
                            self._redirect(migu_ticket(f"m:{top_pid}", settings))
                            return
                        except Exception as exc:  # noqa: BLE001 - 换票失败回落其它线路
                            state.log(f"[咪咕] {name} 换票失败，改用其它源：{exc}")

            try:
                text, stale, seq = state.relay.playlist_info(key, self._base())
            except RelayError as exc:
                self._json({"error": str(exc), "channel": name}, 502)
                return
            except Exception as exc:  # noqa: BLE001 - 失败要给出可读原因
                self._json({"error": f"{type(exc).__name__}: {exc}", "channel": name}, 502)
                return
            # VLC / libVLC 内核（观屿、Ceau 等）读到清单最后一项就停，最新一整片
            # 要等下一片列出来才播得上，上游出长片时就「停一两秒再自己顺回去」。
            # 给这类播放器垫一层空片，让末项永远是垫片、并压低 TARGETDURATION。
            # 只改发给播放器的那份清单，不碰 relay 的窗口和上游取流。
            pad_mode = settings.pad_mode
            ua = self.headers.get("User-Agent") or ""
            did_pad = False
            if pad_mode == "on" or (pad_mode == "auto" and is_vlc_ua(ua)):
                pad_url = f"{self._base()}/pad.ts"
                padded = padded_playlist(text, pad_url)
                if padded is not None:
                    text = padded
                    did_pad = True

            # 这几个头是给人排查用的：F12 里看 /live/xxx.m3u8 的响应，
            # Seq 一直不动就是上游卡住了，Stale=1 表示这次给的是旧的那份，
            # Segs 是这次窗口里带了几片——正常应该在 3~8 之间往上长。
            self._send(
                200,
                text.encode("utf-8"),
                "application/vnd.apple.mpegurl; charset=utf-8",
                {
                    "X-HD-Seq": str(seq),
                    "X-HD-Stale": "1" if stale else "0",
                    "X-HD-Segs": str(text.count("#EXTINF")),
                    "X-HD-Pad": "1" if did_pad else "0",
                },
                cache="no-store",
            )

        def _playlist(self, *, kids_only: bool = False) -> None:
            """按请求现拼订阅列表。

            为什么不直接发 result.m3u：那份文件在更新时就把地址写死了，
            改了对外地址、套了反代都得等下一轮更新才生效。现拼的成本是
            几十个频道做一次字符串拼接，可以忽略。

            ``kids_only`` 走少儿派生视图（/kids.m3u）：只留少儿台、分组名统一
            显示成「少儿频道」。原始数据一行不改，也不影响普通订阅。
            """
            result = load_result(settings) or {}
            channels = _channels_from_result(result)
            if not channels:
                self._json(
                    {"error": "结果尚未生成，请先更新一次",
                     "hint": "面板右上角「立即更新」，或者等首轮采集跑完"},
                    404,
                )
                return
            # 面板上隐藏掉的台不出现在订阅里（原始数据还在，随时能恢复）
            channels = _visible(channels, settings.hidden)
            # 关掉的源模块整组摘掉。这一步按请求现做，改开关不用等下一轮更新。
            channels = _module_visible(channels, settings)
            # 少儿分组里的动画轮播单独开关；其它模块不受影响。
            channels = _kids_visible(channels, settings)

            display_group = ""
            if kids_only:
                if not settings.kids_enabled:
                    self._json(
                        {"error": "少儿频道订阅已在面板关闭",
                         "hint": "数据源页 → 少儿频道 → 打开开关"},
                        404,
                    )
                    return
                channels = _kids_only(channels, settings)
                if not channels:
                    self._json(
                        {"error": "还没有采集到少儿频道",
                         "hint": "先更新一次数据；或检查少儿频道模块是否已开启"},
                        404,
                    )
                    return
                from .kids import KIDS_MODULE
                display_group = KIDS_MODULE

            base = self._base()
            logo_base = settings.logo_base or (f"{base}/logo" if base else "")
            epg_url = settings.epg_url or (f"{base}/epg.xml" if base else "")
            text, _ = writers.render_m3u(
                channels,
                epg_url=epg_url,
                logo_base=logo_base,
                relay_base=base,
                tvg_ids=state.epg.mapping(),
                lines_per_channel=settings.lines_per_channel,
                failover=settings.failover,
                group_override=display_group,
            )
            self._send(
                200,
                text.encode("utf-8"),
                M3U_TYPE,
                {"Content-Disposition": 'inline; filename="hd-iptv.m3u"'},
            )

        def _relay(self, path: str) -> None:
            """/relay/<slug>/p/<token>：转发子清单、分片和密钥。"""
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[0] != "relay" or parts[2] != "p":
                self._json({"error": "地址格式应为 /relay/<slug>/p/<token>"}, 400)
                return
            # slug 里的 o: 会被播放器/浏览器编码成 o%3A，还原后再交出去，
            # 否则中转层认不出这是官方源的静态 slug
            try:
                code, ctype, body, extra = state.relay.resource(
                    unquote(parts[1]), parts[3], self.headers.get("Range", ""), self._base()
                )
            except RelayError as exc:
                self._json({"error": str(exc)}, 502)
                return
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return
            self._send(code, body, ctype, extra)

        def _epg(self) -> None:
            result = load_result(settings) or {}
            names = [str(c.get("name") or "") for c in result.get("channels", []) or []]
            names = [n for n in names if n]
            if not names:
                from .normalize import load_channels

                names = [c.name for c in load_channels(settings.channels_file)]
            data = state.epg.xml(names)
            if not data:
                self._json({"error": "节目单尚未生成", "detail": state.epg.error}, 404)
                return
            self._send(200, data, "application/xml; charset=utf-8", cache="public, max-age=1800")

        def _logo(self, path: str) -> None:
            name = unquote(path[len("/logo/") :])
            if name.lower().endswith(".png"):
                name = name[:-4]
            if not name:
                self._json({"error": "缺少频道名"}, 400)
                return
            data = state.logos.get(name)
            self._send(200, data, "image/png", cache="public, max-age=86400")

        _STATIC_TYPES = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }

        def _static(self, path: str) -> None:
            """面板的静态资源，限定在 web/ 目录内。

            之前是两个写死的文件，加内置播放器要带 hls.js（约 400KB），
            写死就太蠢了。这里只放行白名单后缀，并且挡住 ../ 穿越。
            """
            rel = unquote(path[len("/static/") :]).lstrip("/")
            ctype = self._STATIC_TYPES.get(Path(rel).suffix.lower())
            if not rel or not ctype:
                self._json({"error": "not found", "path": path}, 404)
                return
            target = (WEB_DIR / rel).resolve()
            try:
                target.relative_to(WEB_DIR.resolve())
            except ValueError:
                self._json({"error": "not found", "path": path}, 404)
                return
            if not target.is_file():
                self._json({"error": "not found", "path": path}, 404)
                return
            # 面板脚本改动频繁，必须让浏览器每次都回来问；第三方库体积大、不常变
            cache = "public, max-age=604800" if rel.startswith("vendor/") else "no-cache"
            self._send(200, target.read_bytes(), ctype, cache=cache)

        def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷屏
            return

        def handle_one_request(self) -> None:
            # 播放器切台、关页面时直接断开，是常态；不接住会往 stderr 刷一大段栈
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        # ---------------- 路由 ----------------
        def do_GET(self) -> None:  # noqa: N802
            path = self._require()
            if path is None:
                return

            if path in ("/", "/index.html"):
                self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/pad.ts":
                # 垫片分片：纯 TS 空包，供 VLC 清单的「末项垫片」引用。走 _require
                # 校验（URL 带密码段），不受 pad_mode 限制——旧清单可能还引用着它。
                self._send(200, pad_body(), "video/mp2t", cache="public, max-age=86400")
            elif path.startswith("/static/"):
                self._static(path)
            elif path in ("/m3u", "/m3u8", "/playlist.m3u", "/list.m3u", "/result.m3u"):
                # .m3u8 是 HLS 媒体清单的扩展名，播放器会当成「单个直播流」解析，
                # 然后报「播放格式不受支持」——所以这个入口改成跳转到 .m3u
                if path == "/m3u8":
                    # 跳转目标要自己带上密码段：_redirect 给的是绝对路径，
                    # 不补的话浏览器会被送到 /playlist.m3u，当场被密码拦住。
                    self._redirect(f"{secret_prefix(settings.secret)}/playlist.m3u")
                else:
                    self._playlist()
            elif path in ("/kids.m3u", "/kids"):
                # 老版少儿专属地址的兼容别名，面板已不再展示；新用法是
                # 主订阅里按模块开关出现的「少儿频道」分组。
                self._playlist(kids_only=True)
            elif path in ("/txt", "/playlist.txt", "/result.txt"):
                result = load_result(settings) or {}
                channels = _kids_visible(
                    _module_visible(
                        _visible(_channels_from_result(result), settings.hidden), settings
                    ),
                    settings,
                )
                if not channels:
                    self._json({"error": "结果尚未生成，请先更新一次"}, 404)
                    return
                text, _ = writers.render_txt(channels, relay_base=self._base(),
                                             lines_per_channel=settings.lines_per_channel,
                                             failover=settings.failover)
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            elif path in ("/json", "/result.json"):
                result = load_result(settings)
                if not result:
                    self._json({"error": "结果尚未生成，请先更新一次"}, 404)
                    return
                hidden = set(settings.hidden)
                if hidden:
                    rows = [c for c in result.get("channels", []) or [] if c.get("name") not in hidden]
                    result = dict(result, channels=rows, hidden=sorted(hidden))
                self._json(result)
            elif path == "/epg.xml":
                self._epg()
            elif path.startswith("/logo/"):
                self._logo(path)
            elif path == "/api/status":
                result = load_result(settings) or {}
                relay_stats = state.relay.stats()
                pre = secret_prefix(settings.secret)
                self._json(
                    {
                        "version": __version__,
                        "running": state.running,
                        "last_started": state.last_started,
                        "last_finished": state.last_finished,
                        "last_error": state.last_error,
                        "generated_at": result.get("generated_at", ""),
                        "report": result.get("report", {}),
                        "logs": state.logs[-40:],
                        "settings": state.current_settings(result),
                        "base": self._base(),
                        # 这条宽带是哪里的。只用于面板提示，采集流程不看它。
                        "region": state.region or {},
                        "epg": state.epg.stats(),
                        "logos": state.logos.stats(),
                        # 上游被限流时面板要能说清楚，而不是只让用户看到画面卡住
                        "relay": {
                            "mode": relay_stats.get("mode", "direct"),
                            "failures": relay_stats["failures"],
                            "stale_served": relay_stats["stale_served"],
                            "throttled": relay_stats.get("throttled", 0),
                            "blocked_for": relay_stats.get("blocked_for", 0),
                            "plays": len(relay_stats["cached"]),
                            "errors": list(relay_stats["errors"].values())[:3],
                            "segments": relay_stats["segments"],
                        },
                        "endpoints": {
                            "m3u": f"{pre}/playlist.m3u",
                            "txt": f"{pre}/txt",
                            "json": f"{pre}/json",
                            "epg": f"{pre}/epg.xml",
                            "logo": f"{pre}/logo/<频道名>.png",
                            "live": f"{pre}/live/<slug>.m3u8",
                            "live_list": f"{pre}/api/live",
                            "relay": f"{pre}/relay/<slug>/p/<token>",
                            "settings": f"{pre}/api/settings",
                            "hidden": f"{pre}/api/hidden",
                            "version": f"{pre}/api/version?check=1",
                        },
                    }
                )
            elif path == "/api/settings":
                self._json({"settings": state.current_settings(load_result(settings) or {}), "epg": state.epg.stats()})
            elif path == "/api/region/options":
                from .region import ISP_NAMES, PROVINCES

                self._json({
                    "provinces": [{"code": k, "name": v} for k, v in PROVINCES.items()],
                    "isps": [{"code": k, "name": v} for k, v in ISP_NAMES.items()],
                })
            elif path == "/api/channels":
                result = load_result(settings) or {}
                hidden = set(settings.hidden)
                disabled = _module_disabled_names(settings)
                rows = [
                    dict(row, hidden=row.get("name") in hidden)
                    for row in (result.get("channels", []) or [])
                    # 与订阅一致：关掉的模块整组不返回，面板只显示启用的源
                    if _row_module_visible(row, disabled)
                    and _row_kids_visible(row, settings)
                ]
                self._json({
                    "generated_at": result.get("generated_at", ""),
                    "channels": rows,
                    "hidden": sorted(hidden),
                })
            elif path == "/health":
                self._json({"ok": True, "running": state.running})
            elif path == "/api/version":
                from .updater import check

                force = "1" in parse_qs(urlsplit(self.path).query).get("check", [])
                self._json(check(__version__, force=force))
            elif path == "/api/relay":
                self._json(state.relay.stats())
            elif path.startswith("/relay/"):
                self._relay(path)
            elif path.startswith(("/live/", "/ysp/")) and path.endswith(".m3u8"):
                slug = path.rsplit("/", 1)[-1][: -len(".m3u8")]
                self._live(slug)
            elif path == "/api/live":
                from . import ysp

                # 先列「当前这份结果里真在用的地址」——官方源接进来之后，央视
                # 走的是 /live/o:... 这类静态 slug，只列央视频那张表等于给了
                # 一堆已经不是首选的地址，复制过去当然对不上号。
                rows: list[dict] = []
                names: set[str] = set()
                result = load_result(settings) or {}
                for row in result.get("channels", []) or []:
                    name = str(row.get("name") or "")
                    best = next((s for s in (row.get("streams") or []) if s.get("slug")), None)
                    if not name or not best:
                        continue
                    slug = str(best["slug"])
                    rows.append({
                        "slug": slug, "name": name, "source": "当前列表",
                        "url": f"/live/{slug}.m3u8",
                    })
                    names.add(name)
                for row in ysp.CHANNELS:
                    slug, name = row[0], row[1]
                    if name in names:
                        continue
                    rows.append({"slug": slug, "name": name, "source": "央视频",
                                 "url": f"/live/{slug}.m3u8"})
                self._json({
                    "hint": "把 /live/<slug>.m3u8 填进播放器，由本机转发，任何播放器都能放",
                    "channels": rows,
                })
            else:
                self._json({"error": "not found", "path": path}, 404)

        def do_HEAD(self) -> None:  # noqa: N802
            """HEAD 探活本地应答，绝不为它换票或取清单。

            播放器（以及面板里的「检测可用性」）会用 HEAD 把每个台问一遍，
            一台一张票正好撞上按出口 IP 的频率限制。HEAD 只需要状态码和头，
            本地回 200 空体即可；真正要看的 GET 才走上游。
            """
            path = self._require()
            if path is None:
                return
            if path.startswith(("/live/", "/ysp/")) and path.endswith(".m3u8"):
                slug = path.rsplit("/", 1)[-1][: -len(".m3u8")]
                if live_target(slug) is not None:
                    self._send(200, b"", "application/vnd.apple.mpegurl; charset=utf-8",
                               cache="no-store")
                    return
            self.do_GET()

        def _body(self) -> dict | None:
            """读 JSON 请求体；不合法时已经回过 400，返回 None。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(min(length, 64 * 1024)) if length else b"{}"
                values = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as exc:
                self._json({"error": f"请求体不是合法 JSON：{exc}"}, 400)
                return None
            if not isinstance(values, dict):
                self._json({"error": "需要一个 JSON 对象"}, 400)
                return None
            return values

        def do_POST(self) -> None:  # noqa: N802
            path = self._require()
            if path is None:
                return
            if path == "/api/update":
                # 可选 body：{"module": "运营商官方源"} 只重采这一个模块。
                # 顶栏全量按钮和老客户端 POST 空 body，都按全量处理。
                values = self._body()
                module = str((values or {}).get("module") or "").strip()
                started = state.trigger(module)
                if started:
                    message = f"已开始只更新「{module}」" if module else "已开始更新"
                else:
                    message = "已有任务在执行"
                self._json({"started": started, "message": message, "module": module})
            elif path == "/api/settings":
                values = self._body()
                if values is None:
                    return
                try:
                    updated = state.apply_settings(values)
                except ValueError as exc:
                    # 典型就是密码带了空格、斜杠、中文——回 400 说清楚，
                    # 别静默改成别的字符串让人下次进不去
                    self._json({"error": str(exc)}, 400)
                    return
                # 密码不进日志：面板日志会显示在页面上，也可能被人截图发出去
                shown = {
                    k: ("***" if k in ("secret", "migu_token", "migu_user_id") and v else v)
                    for k, v in updated.items()
                }
                state.log(f"[设置] 已更新 {', '.join(f'{k}={v}' for k, v in shown.items())}")
                self._json({"ok": True, "settings": updated})
            elif path == "/api/region/probe":
                values = self._body()
                if values is None:
                    return
                province = str(values.get("province") or "").strip().lower()
                isp = str(values.get("isp") or "").strip().lower()
                if not province or not isp:
                    self._json({"error": "需要同时提供省份和运营商"}, 400)
                    return
                from .ispcheck import check as isp_check, list_note, verdict

                # 面板上是「点一下等几秒」，所以样本和超时都压小：
                # 只要看出这省这家有没有公开入口、通不通即可，不做全量探测。
                result = isp_check(province, isp, timeout=2.0, sample=3, list_timeout=6.0)
                result["note"] = list_note(result)
                result["verdict"] = verdict({}, result)
                self._json(result)
            elif path == "/api/region/scan":
                values = self._body()
                if values is None:
                    return
                province = str(values.get("province") or "").strip().lower()
                if not province:
                    self._json({"error": "缺少省份"}, 400)
                    return
                from .ispcheck import scan_province

                self._json(scan_province(province, timeout=4.0))
            elif path == "/api/hidden":
                values = self._body()
                if values is None:
                    return
                names = values.get("names") or values.get("name") or []
                flag = values.get("hidden", True)
                if isinstance(flag, str):
                    flag = flag.strip().lower() not in ("", "0", "false", "no", "off")
                updated = state.set_hidden(names if isinstance(names, list) else [names], bool(flag))
                state.log(
                    f"[隐藏] {'隐藏' if flag else '恢复'} {', '.join(names if isinstance(names, list) else [names])}"
                    f"，当前隐藏 {len(updated)} 个台"
                )
                self._json({"ok": True, "hidden": updated})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def _scheduler(state: State) -> None:
    """定时更新。间隔可以被面板改掉，所以每轮都重新读一次。"""
    while True:
        interval = state.settings.interval_hours
        if interval <= 0:
            time.sleep(15)
            continue
        state.trigger()
        # 宽带识别只在启动时跑过一次，缓存一天就过期，容器长期不重启时
        # 面板那行会消失、自动按省取源也拿不到省份。每轮顺手刷一次；
        # detect() 命中缓存就直接返回，不会每轮都去查归属地接口。
        state.refresh_region()
        finish_at = time.time() + interval * 3600
        while time.time() < finish_at:
            # 分片睡眠：面板把间隔改小以后最多 15 秒就生效
            time.sleep(min(15.0, max(1.0, finish_at - time.time())))
            if abs(state.settings.interval_hours - interval) > 1e-6:
                break


class Server(ThreadingHTTPServer):
    """并发播放要留够线程与排队位。

    一个频道同时有子清单、若干分片、密钥在跑，多开几个台就上两位数连接。
    默认 backlog 只有 5，播放器会开始报「连接被拒绝」。
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def serve(settings: Settings, host: str = "0.0.0.0", port: int = 9797) -> None:
    state = State(settings)
    report = load_result(settings).get("report", {})
    if report:
        state.report = report
        state.last_finished = report.get("finished_at", "")

    if settings.run_on_start:
        state.trigger()
    else:
        # 升级前的结果里没有少儿分组，但少儿默认开。后台单独抓一轮，
        # 用户更新镜像后不用等下一个定时全量周期，订阅里就能看到少儿。
        from .kids import KIDS_MODULE

        old = load_result(settings) or {}
        if settings.kids_enabled and not any(
            display_module(str(c.get("group") or "")) == KIDS_MODULE
            for c in (old.get("channels") or [])
        ):
            state.trigger(KIDS_MODULE)

    threading.Thread(target=_scheduler, args=(state,), daemon=True).start()
    # 识别宽带放后台：面板第一屏就能打开，几秒后那一行自己补上
    threading.Thread(target=state.refresh_region, daemon=True).start()
    interval = settings.interval_hours
    state.log(
        f"定时更新：每 {interval:g} 小时一次" if interval > 0 else "定时更新：已关闭（面板里可以打开）"
    )

    httpd = Server((host, port), make_handler(state))
    state.log(
        f"服务已启动：http://{host}:{port}{secret_prefix(settings.secret) or '/'}"
        f"  （订阅地址 {secret_prefix(settings.secret)}/playlist.m3u）"
    )
    if settings.secret:
        state.log("已开启访问密码：所有地址都要带 /<密码>/ 这一段，不带会回「身份认证失败」")
    state.log(
        f"分片缓存上限 {int(settings.relay_cache_mb)} MB，清单预取 {settings.playlist_ttl:g} 秒一次，"
        "多客户端共用一个上游连接池"
    )
    if not settings.public_url:
        state.log("提示：未设置对外地址，列表按请求的 Host 现拼；套了反代会自动用外网域名")
    # 画质上限被环境变量压住时给一句明说：配置文件里改对了也不生效，
    # 而这一项直接决定「4K 官方源要不要」，卡在 1080 就是卫视退回空壳源。
    if os.environ.get("HDIPTV_MAX_HEIGHT") and settings.max_height <= 1080:
        state.log(
            f"注意：环境变量 HDIPTV_MAX_HEIGHT={settings.max_height} 把画质上限压住了，"
            "只有 4K 一路的官方源（芒果、浙江广电）会被整条丢掉；"
            "不需要就删掉这条环境变量，或改成 2160"
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        state.log("收到退出信号")
    finally:
        httpd.server_close()


__all__ = ["serve", "State", "make_handler"]
