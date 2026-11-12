"""识别这条宽带是哪里的：出口 IP、省份、运营商、IPv6 能不能出去。

**只做识别和展示，不改采集逻辑。** 所以这里任何一步失败都只影响面板上那一行
文字，不会让更新失败、也不会让已经能播的台变少。

为什么要知道这个：项目的运营商官方源是按省隔离的（湖北移动的 CDN 只服务湖北
移动的出口 IP，换个省直接 403）。用户在面板上看到「湖北 · 中国移动 · 无 IPv6」，
就知道自己为什么只有 56 台、以及该关掉哪个源，不用去翻日志猜。

判断依据按可靠性排序：

1. ``myip.ipip.net`` —— 国内服务，快，返回中文，一次给全 IP / 省 / 运营商；
2. ``ipinfo.io`` —— 国外服务，国内偶尔慢，返回英文 region 和 AS 组织名；
3. 手动指定 —— ``config.toml`` 里写 ``region = "hubei"``，IP 库判错时的兜底。

结果缓存 ``region.json`` 一天，避免每次启动都去敲外部接口。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.request
from pathlib import Path

CACHE_NAME = "region.json"
DEFAULT_TTL = 24 * 3600.0          # 一天换一次足够
DEFAULT_TIMEOUT = 6.0

# 拼音代码 -> 中文名。代码刻意与公开源目录名保持一致（Unicast/<code>/<isp>.txt），
# 下一步做自动选源时可以直接拼路径，不用再翻译一次。
PROVINCES: dict[str, str] = {
    "anhui": "安徽", "beijing": "北京", "chongqing": "重庆", "fujian": "福建",
    "gansu": "甘肃", "guangdong": "广东", "guangxi": "广西", "guizhou": "贵州",
    "hainan": "海南", "hebei": "河北", "heilongjiang": "黑龙江", "henan": "河南",
    "hubei": "湖北", "hunan": "湖南", "jiangsu": "江苏", "jiangxi": "江西",
    "jilin": "吉林", "liaoning": "辽宁", "neimenggu": "内蒙古", "ningxia": "宁夏",
    "qinghai": "青海", "shaanxi": "陕西", "shandong": "山东", "shanghai": "上海",
    "shanxi": "山西", "sichuan": "四川", "tianjin": "天津", "xinjiang": "新疆",
    "xizang": "西藏", "yunnan": "云南", "zhejiang": "浙江",
}

# 各家 IP 库对同一个省的写法都不一样：中文、拼音、英文、还有内蒙古这种历史拼法。
_ALIASES: dict[str, str] = {}
for _code, _cn in PROVINCES.items():
    _ALIASES[_code] = _code
    _ALIASES[_cn] = _code
    _ALIASES[_cn.replace("省", "").replace("市", "").replace("自治区", "")] = _code
_ALIASES.update({
    "neimeng": "neimenggu", "nei mongol": "neimenggu", "inner mongolia": "neimenggu",
    "xizang autonomous region": "xizang", "tibet": "xizang",
    "guangxi zhuang": "guangxi", "ningxia hui": "ningxia", "xinjiang uygur": "xinjiang",
    "hong kong": "", "macao": "macao", "macau": "macao", "taiwan": "",
})

# 运营商：从各家返回里能看到的写法，全收进来。
_ISPS: dict[str, str] = {
    "mobile": "mobile", "移动": "mobile", "中国移动": "mobile",
    "china mobile": "mobile", "cmcc": "mobile", "cmnet": "mobile",
    "telecom": "telecom", "电信": "telecom", "中国电信": "telecom",
    "china telecom": "telecom", "chinanet": "telecom", "ctc": "telecom",
    "unicom": "unicom", "联通": "unicom", "中国联通": "unicom",
    "china unicom": "unicom", "china169": "unicom",
    "broadnet": "broadnet", "广电": "broadnet", "中国广电": "broadnet",
    "radio and television": "broadnet",
}
ISP_NAMES = {
    "mobile": "中国移动", "telecom": "中国电信",
    "unicom": "中国联通", "broadnet": "中国广电",
}

# 纯 IPv6 的公共 DNS，用来判断「这台机器能不能走 IPv6 出去」。
# 端口用 53/tcp —— 几乎所有网络都放行，比 HTTP 目标更少误判。
_IPV6_TARGETS = (
    ("2400:3200::1", 53),           # 阿里
    ("2402:4e00::", 53),            # DNSPod
    ("2606:4700:4700::1111", 53),   # Cloudflare
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def province_of(text: str) -> str:
    """把各种写法的省名换成拼音代码，认不出来就返回空串。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    key = _norm(raw)
    if key in _ALIASES:
        return _ALIASES[key]
    # 「湖北省武汉市」「Hubei, China」这类，取第一个匹配上的
    for alias, code in _ALIASES.items():
        if alias and alias in key:
            return code
    return ""


def isp_of(text: str) -> str:
    """从运营商名称 / AS 组织名里认运营商。"""
    key = _norm(text)
    if not key:
        return ""
    # 长的先匹配，"china mobile" 不能被 "mobile" 抢先也没关系，结果一样
    for alias in sorted(_ISPS, key=len, reverse=True):
        if alias in key:
            return _ISPS[alias]
    return ""


def ipv6_available(budget: float = 3.0) -> bool:
    """本机能不能真的连上 IPv6 地址。

    只看「有没有 IPv6 地址」不够——容器里常年有链路本地地址但路由不通，
    所以这里真去连一次。三个目标共用 ``budget`` 秒，最坏也就等这么久，
    不会因为某个目标黑洞而把启动拖住。
    """
    deadline = time.monotonic() + max(0.5, budget)
    for host, port in _IPV6_TARGETS:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
                sock.settimeout(min(1.5, left))
                sock.connect((host, port))
                return True
        except OSError:
            continue
    return False


def _fetch(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(64 * 1024).decode("utf-8", "replace")


_IPIP_RE = re.compile(r"当前\s*IP[：:]\s*(\S+)[\s\S]*?来自[于]?[：:]\s*(.+)")


def _by_ipip(timeout: float) -> dict:
    """myip.ipip.net：`当前 IP：<ip>  来自于：中国 湖北 武汉  移动`"""
    text = _fetch("https://myip.ipip.net/", timeout)
    m = _IPIP_RE.search(text)
    if not m:
        return {}
    ip = m.group(1).strip()
    tail = m.group(2).replace("\r", " ").replace("\n", " ").strip()
    parts = [p for p in re.split(r"[\s,，]+", tail) if p]
    where = " ".join(parts)
    return {
        "ip": ip,
        "province": province_of(where),
        "isp": isp_of(where),
        "source": "ipip",
        "raw": text.strip()[:120],
    }


def _by_ipinfo(timeout: float) -> dict:
    """ipinfo.io：region 是英文省名，org 是 `AS9808 China Mobile ...`。"""
    data = json.loads(_fetch("https://ipinfo.io/json", timeout))
    if not isinstance(data, dict):
        return {}
    region = " ".join(str(data.get(k) or "") for k in ("region", "city"))
    org = " ".join(str(data.get(k) or "") for k in ("org", "as", "isp"))
    return {
        "ip": str(data.get("ip") or ""),
        "province": province_of(region),
        "isp": isp_of(org),
        "source": "ipinfo",
        "raw": (str(data.get("org") or "") + " / " + str(data.get("region") or ""))[:120],
    }


def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / CACHE_NAME


def read_cache(data_dir: Path, ttl: float = DEFAULT_TTL) -> dict | None:
    try:
        data = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        age = time.time() - float(data.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ttl:
        return None
    return data


def write_cache(data_dir: Path, info: dict) -> None:
    try:
        path = _cache_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def detect(
    data_dir: Path,
    *,
    manual_province: str = "",
    manual_isp: str = "",
    ttl: float = DEFAULT_TTL,
    timeout: float = DEFAULT_TIMEOUT,
    force: bool = False,
) -> dict:
    """返回这条宽带的信息。认不出来也返回一个带 ``ok: False`` 的结果，不抛异常。

    字段：
      ok        是否至少拿到了省份或运营商
      ip        出口地址（可能是 IPv6）
      province  拼音代码，例 ``hubei``；认不出为空
      province_name 中文省名
      isp       ``mobile`` / ``telecom`` / ``unicom`` / ``broadnet`` / 空
      isp_name  中文运营商名
      ipv6      能不能连上 IPv6
      source    哪个接口给的（ipip / ipinfo / manual / cache）
      manual    是否人为指定
      note      给人看的一句话
    """
    manual_province = province_of(manual_province) or (manual_province or "").strip().lower()
    manual_isp = isp_of(manual_isp) or (manual_isp or "").strip().lower()
    if manual_province and manual_province not in PROVINCES:
        manual_province = ""

    if not force and not (manual_province or manual_isp):
        cached = read_cache(data_dir, ttl)
        if cached:
            cached["source"] = "cache"
            cached["note"] = _note(cached)
            return cached

    info: dict = {"ok": False, "ip": "", "province": "", "isp": "", "source": ""}
    errors: list[str] = []

    if manual_province or manual_isp:
        info["source"] = "manual"
    else:
        for name, fn in (("ipip", _by_ipip), ("ipinfo", _by_ipinfo)):
            got: dict = {}
            last = f"{name}: 未知原因"
            # 偶尔一次 SSL 抖动很常见（尤其是国内访问境外接口时），
            # 重试一次往往就过；两次都不行才换下一个源。
            for attempt in range(2):
                try:
                    got = fn(timeout)
                except Exception as exc:                 # noqa: BLE001 - 任何失败都换下一个
                    got = {}
                    last = f"{name}: {type(exc).__name__}"
                    if attempt == 0:
                        time.sleep(0.3)
                    continue
                if got.get("province") or got.get("isp"):
                    break
                last = f"{name}: 有返回但认不出"
                break
            if got.get("province") or got.get("isp"):
                info.update(got)
                break
            errors.append(last)

    if manual_province:
        info["province"] = manual_province
    if manual_isp:
        info["isp"] = manual_isp
    if manual_province or manual_isp:
        info["manual"] = True

    info["province_name"] = PROVINCES.get(info.get("province", ""), "")
    info["isp_name"] = ISP_NAMES.get(info.get("isp", ""), "")
    info["ok"] = bool(info["province_name"] or info["isp_name"])
    info["ipv6"] = ipv6_available()
    info["at"] = time.time()
    if errors:
        info["errors"] = errors
    info["note"] = _note(info)

    # 手动指定的不落缓存，否则改了配置还得等一天
    if not info.get("manual"):
        write_cache(data_dir, info)
    return info


def _note(info: dict) -> str:
    """面板上显示的一句话。"""
    where = info.get("province_name") or ""
    isp = info.get("isp_name") or ""
    if not where and not isp:
        return "没能识别出运营商（不影响使用，只是官方源可能不匹配）"
    head = " · ".join(x for x in (where, isp) if x)
    tail = "IPv6 可用" if info.get("ipv6") else "无 IPv6 出口"
    if info.get("manual"):
        head += "（手动指定）"
    elif info.get("source") == "cache":
        head += "（缓存）"
    return f"{head} · {tail}"


__all__ = [
    "PROVINCES", "ISP_NAMES", "province_of", "isp_of", "ipv6_available",
    "detect", "read_cache", "write_cache",
]
