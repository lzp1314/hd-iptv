"""按宽带所在省份，自动挂一份本省运营商的单播清单。

公开源那几份清单是全国通用的，但央视频要现换票、第三方转播站会夹广告；
真正稳的是自家宽带的 IPTV 边缘。项目内置的那份只服务湖北移动，外地用户
以前得自己去找清单、自己写 sources.toml。这里把这一步自动化。

几条刻意守住的边界：

* **清单运行时拉，不打包进仓库** —— 上游 ``xisohi/CHINA-IPTV`` 没有 LICENSE。
* **只认 http(s) 公网单播** —— 组播（rtp/udp）、别人路由器里的
  ``192.168.x`` udpxy 地址、只有频道名的占位清单全部丢掉，不留灰台。
* **湖北移动跳过** —— 项目已内置 91 台的完整清单，自动拉一份重复的没意义。
* **采集流程之外不联网** —— ``load_sources()`` 只看本地缓存文件在不在；
  真正去拉清单只在 ``run_update()`` 开头调一次 ``refresh()``，且有 24h 缓存。
  这样测试、面板、CLI 都不会因为打开这个功能而多一次外网请求。
* **失败一律静默** —— 上游挂了/没这份清单/网络不通，只写一行日志，
  其余源照常采集，绝不把整轮更新拖垮。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import region
from .collectors import parse_list
from .ispcheck import fetch_list
from .normalize import ChannelMatcher, load_channels

AUTO_SOURCE_NAME = "本省自动源"

CACHE_DIR = "auto-region"
CACHE_TTL = 24 * 3600.0        # 清单一天拉一次就够，地址不会天天变
MAX_ENTRIES = 80               # 慢探一条要 1.2 秒，别让一轮采集被本省清单拖到超时

# 省/运营商这种东西几乎不会变，而 region.json 是给面板显示用的、只缓存一天。
# 这里放宽到一年：容器长期不重启时，自动源不该因为面板缓存过期就失效。
_REGION_CACHE_TTL = 365 * 86400.0

_PRIVATE_HOST = re.compile(
    r"^(?:"
    r"localhost|0\.|127\.|10\.|192\.168\.|169\.254\.|"
    r"172\.(?:1[6-9]|2\d|3[01])\.|"
    r"::1|fe[89ab]|f[cd]"      # IPv6 本地 / 链路本地 / 唯一本地
    r")"
)


def _public_host(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host.startswith("[") or ":" in host:
        # IPv6 公网边缘（2409:...）要留着，湖北移动那类就是这种
        return not _PRIVATE_HOST.match(host)
    if _PRIVATE_HOST.match(host):
        return False
    return True


def region_of(settings) -> tuple[str, str]:
    """拿到 (省份拼音, 运营商)。手动指定优先，其次读 region.json 缓存。

    不调用 ``region.detect()``：那会去查 IP 归属地接口，等于每次采集都联网，
    还可能因为接口抖动让整轮更新变慢。识别是服务端启动时后台做的。
    """
    province = region.province_of(settings.region_hint) or ""
    isp = region.isp_of(settings.isp_hint) or ""
    cached = region.read_cache(settings.data_dir, _REGION_CACHE_TTL) or {}
    province = province or str(cached.get("province") or "").strip().lower()
    isp = isp or str(cached.get("isp") or "").strip().lower()
    return province, isp


def cache_path(settings, province: str, isp: str) -> Path:
    return settings.data_dir / CACHE_DIR / f"{province}-{isp}.txt"


def clean_list(text: str, limit: int = MAX_ENTRIES, match=None) -> list[tuple[str, str]]:
    """把清单收敛成 (频道名, 公网 http(s) 地址)，去重并限量。

    ``parse_list`` 自动识别 m3u / txt；txt 解析器只收 http(s)/rtmp/rtsp，
    rtp://239.x 这类组播天然进不来，这里再补一道公网地址过滤。

    ``match`` 给了就只留能对上频道模板的名字。本省清单里往往大半是地方台，
    与其让它们进串行慢探（一条 1.2 秒）再被 ``_assign()`` 丢掉，不如在
    写缓存前就筛干净 —— 这也是「只补央视/卫视线路」这条选择的落点。
    """
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for stream in parse_list(text or ""):
        url = str(getattr(stream, "url", "") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            continue
        if not _public_host(host) or url in seen:
            continue
        name = str(getattr(stream, "raw_name", "") or "").strip()
        # 名字里带逗号会破坏后面写回的 txt 格式
        name = re.sub(r"[\r\n,]", " ", name).strip()
        alt = str(getattr(stream, "alt_name", "") or "").strip()
        if match is not None and not (match(name) or (alt and match(alt))):
            continue
        seen.add(url)
        rows.append((name, url))
        if len(rows) >= limit:
            break
    return rows


def _template_match(settings):
    """按频道模板给出「这个名字要不要留」的判断；模板读不到就不过滤。"""
    try:
        channels = load_channels(settings.channels_file)
        if not channels:
            return None
        matcher = ChannelMatcher(channels)
    except Exception:                        # noqa: BLE001 - 模板异常不该拖垮采集
        return None

    def match(name: str) -> bool:
        try:
            return matcher.match(name).channel is not None
        except Exception:                    # noqa: BLE001
            return False

    return match


def _serialize(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"{name},{url}" if name else url for name, url in rows) + "\n"


def _write(path: Path, rows: list[tuple[str, str]]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(_serialize(rows), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def _skip_reason(province: str, isp: str) -> str:
    if not province or not isp:
        return "还没识别出本机宽带（或服务刚启动），本轮跳过，识别出来后下轮生效"
    if (province, isp) == ("hubei", "mobile"):
        return "湖北移动已内置运营商源，不重复拉"
    if isp == "broadnet":
        return "广电宽带没有公开单播清单，跳过"
    return ""


def refresh(settings, log=None) -> Path | None:
    """必要时拉一次本省清单，落成本地缓存。只在 ``run_update()`` 里调用。

    返回可用的缓存路径；拉不到但有旧缓存时仍返回旧缓存（宁可用昨天的地址，
    也不要因为上游抖一下就把这一路源整个关掉）。
    """
    log = log or (lambda _m: None)
    if not settings.auto_region:
        return None
    province, isp = region_of(settings)
    reason = _skip_reason(province, isp)
    if reason:
        log(f"[自动源] {reason}")
        return None

    path = cache_path(settings, province, isp)
    try:
        fresh = path.exists() and path.stat().st_size > 0 and (
            time.time() - path.stat().st_mtime < CACHE_TTL
        )
    except OSError:
        fresh = False
    if fresh:
        return path

    # 采集流程里的一天一次，超时给短一点，别把整轮更新拖住（两个镜像各重试一次）
    text, state = fetch_list(province, isp, timeout=6.0)
    if state == "ok":
        rows = clean_list(text, match=_template_match(settings))
        if rows:
            if _write(path, rows):
                log(f"[自动源] {province}/{isp} 取到 {len(rows)} 条模板内频道入口，本轮慢探")
                return path
            log("[自动源] 清单写不进 output/（检查目录权限），本轮跳过")
        else:
            log("[自动源] 清单里没有可直连的公网单播入口（可能只有内网地址或频道名），跳过")
    else:
        log(f"[自动源] 本省清单取不到（{state}），本轮跳过，其它源不受影响")

    # 上游临时不可用时，旧缓存比「什么都没有」强
    try:
        if path.exists() and path.stat().st_size > 0:
            log("[自动源] 沿用上次缓存的清单")
            return path
    except OSError:
        pass
    return None


def auto_source(settings, log=None) -> dict | None:
    """给 ``load_sources()`` 用的源配置项。纯本地判断，不发任何网络请求。"""
    if not settings.auto_region:
        return None
    province, isp = region_of(settings)
    if _skip_reason(province, isp):
        return None
    path = cache_path(settings, province, isp)
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
    except OSError:
        return None
    # trust = true：本省 CDN 按出口 IP 记并发，必须走串行慢探，
    # 不能跟着两百多条聚合源一起并发冲（会打成 302 无限循环）。
    return {
        "name": AUTO_SOURCE_NAME,
        "type": "local",
        "path": str(path),          # 绝对路径，load_sources 的相对路径补齐会跳过它
        "trust": True,
        "enabled": True,
    }


__all__ = [
    "AUTO_SOURCE_NAME", "MAX_ENTRIES", "clean_list", "region_of",
    "cache_path", "refresh", "auto_source",
]
