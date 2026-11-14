"""运营商 IPTV 单播源自测：`python -m iptvhd doctor --isp`。

只读诊断，不改配置、不采集、不联网抓取任何节目。回答三个问题：

1. 这台机器现在走的是哪家宽带（省 / 运营商 / 有没有 IPv6）；
2. 本省这一家有没有「像 ``ztecdn.hb.chinamobile.com`` 那样」的统一入口，通不通；
3. 通不了的话，是「本来就没有」，还是「有，但只给组播 / 得自己从机顶盒抓」。

数据来源两处，都能失败：

* 内置的一张小表 ``KNOWN_HOSTS``：这个项目自己验证过、或社区公开整理过的入口；
* 公开清单库的 ``Unicast/<省>/<运营商>.txt``：**运行时读，不打包进仓库**
  （那份清单没有许可证，不能随镜像分发）。取不到就只跑内置表，结论里会写明。

自动接入是 ``autoregion`` 的职责（按识别结果取清单、串行慢探、只给模板内频道
补线路），这里只做只读诊断：把「有没有、通不通」说清楚，方便确认和排障。
"""

from __future__ import annotations

import re
import socket
import time
import urllib.error
import urllib.request

from . import region

# 公开清单的两个入口：raw 常被挡，jsDelivr 兜底。只读，不落盘。
_LIST_URLS = (
    "https://raw.githubusercontent.com/xisohi/CHINA-IPTV/main/Unicast/{province}/{isp}.txt",
    "https://cdn.jsdelivr.net/gh/xisohi/CHINA-IPTV@main/Unicast/{province}/{isp}.txt",
)

# 已知入口：host:port。只放「被公开验证过」的，宁可少也不要瞎猜——
# 猜错会让人以为「我这儿不通」，把本来有用的方向否掉。
KNOWN_HOSTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("hubei", "mobile"): ("ztecdn.hb.chinamobile.com:6060",),
    ("hunan", "mobile"): ("tvgslb.hn.chinamobile.com:8089",),
    ("henan", "mobile"): ("iptv.cdn.ha.chinamobile.com",),
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_URL_RE = re.compile(r"https?://([^/\s:?#]+)(?::(\d+))?")
_PRIVATE_RE = re.compile(r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)")


def _split_host(item: str) -> tuple[str, int]:
    """``host:port`` / ``host`` → ``(host, port)``，默认 80。"""
    raw = (item or "").strip()
    if raw.count(":") == 1:                      # IPv6 会有多个冒号，这里只处理域名
        host, _, port = raw.partition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, 80
    return raw, 80


def is_private_host(host: str) -> bool:
    return bool(_PRIVATE_RE.match(host or ""))


def parse_endpoints(text: str, limit: int = 40,
                    *, include_private: bool = False) -> list[tuple[str, int]]:
    """从 txt / m3u 里抽出 ``(host, port)``，按出现顺序去重。

    只认 http(s) 地址：组播是 ``rtp://`` / ``udp://``，那类要 udpxy 转，
    在这里当成「单播入口」测会永远失败，反而误导。

    默认丢掉 10.x / 192.168.x 这类内网地址——从公网侧连它们只会超时，
    但清单里出现内网地址本身是个有用信号，需要时用 ``include_private`` 留着。
    """
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for m in _URL_RE.finditer(text or ""):
        host = m.group(1).lower()
        port = int(m.group(2) or (443 if m.group(0).startswith("https") else 80))
        if not host or (is_private_host(host) and not include_private):
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


def tcp_probe(host: str, port: int, timeout: float = 4.0) -> tuple[str, float]:
    """连一下 ``host:port``。返回 ``(状态, 毫秒)``。

    状态：``ok`` 连通、``dns`` 解析不了、``timeout`` 超时、``refused`` 拒绝、``error`` 其他。
    一条地址一个总时限，遇到 IPv6 黑洞也不会挂住整轮。
    """
    deadline = time.monotonic() + max(0.5, timeout)
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        return "dns", 0.0
    except Exception:                            # noqa: BLE001 - 解析线程里什么都不许漏
        return "error", 0.0
    if not infos:
        return "dns", 0.0

    last = "error"
    for family, socktype, proto, _, addr in infos:
        left = deadline - time.monotonic()
        if left <= 0:
            return "timeout", 0.0
        started = time.monotonic()
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(left)
                sock.connect(addr)
            return "ok", (time.monotonic() - started) * 1000
        except socket.timeout:
            last = "timeout"
        except ConnectionRefusedError:
            last = "refused"
        except OSError as exc:
            last = "timeout" if getattr(exc, "errno", None) in (110, 113) else "error"
    return last, 0.0


def http_status(host: str, port: int, timeout: float = 5.0) -> int | None:
    """取一次 ``GET /``，只为了看它回什么（200 / 302 / 403）。

    不用 urllib：这类入口经常证书不匹配、或者回一个没有 Content-Length 的 302，
    urllib 会跟着跳到底最后报错，反而看不到状态码。
    """
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    family, socktype, proto, _, addr = infos[0]
    try:
        # 不用 create_connection：它只接受 (host, port)，IPv6 的地址是四元组
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(timeout)
            sock.connect(addr)
            req = f"GET / HTTP/1.0\r\nHost: {host}\r\nUser-Agent: {_UA}\r\nAccept: */*\r\n\r\n"
            sock.sendall(req.encode("ascii", "replace"))
            data = sock.recv(256).decode("iso-8859-1", "replace")
    except OSError:
        return None
    m = re.match(r"HTTP/\d\.\d\s+(\d{3})", data)
    return int(m.group(1)) if m else None


def fetch_list(province: str, isp: str, timeout: float = 8.0) -> tuple[str, str]:
    """取公开清单里本省这一份。返回 ``(文本, 状态)``，状态给人看。

    状态：``ok`` / ``missing``（没有这一份）/ ``fail``（取不到）。

    每个入口失败后再试一次：国内访问 GitHub raw / jsDelivr 经常是首次 TLS
    抖动，隔 0.3 秒重试往往就通。别把偶发抖动当成「本省没有这一份」。
    """
    if not province or not isp:
        return "", "missing"
    missing = False
    for tpl in _LIST_URLS:
        url = tpl.format(province=province, isp=isp)
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read(256 * 1024).decode("utf-8", "replace"), "ok"
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    missing = True
                break                            # 明确的 HTTP 回复，重试也没用
            except Exception:                    # noqa: BLE001 - 抖动/超时，再试一次
                if attempt == 0:
                    time.sleep(0.3)
                    continue
        # 这个入口两次都没成，换下一个镜像
    # 「没这一份」是确定的答案（另一个入口是不是通不影响），别被网络问题盖掉
    return "", "missing" if missing else "fail"


def _row(host: str, port: int, status: str, ms: float, code: int | None) -> dict:
    return {"host": host, "port": port, "status": status, "ms": round(ms), "code": code}


def check(province: str, isp: str, *, timeout: float = 4.0,
          sample: int = 5, list_timeout: float = 8.0) -> dict:
    """跑一遍本省这一家的入口。返回结构化结果，打印交给调用方。"""
    targets = list(KNOWN_HOSTS.get((province, isp), ()))

    text, list_state = fetch_list(province, isp, timeout=list_timeout)
    all_eps = parse_endpoints(text, include_private=True) if list_state == "ok" else []
    public_eps = [e for e in all_eps if not is_private_host(e[0])]
    private_eps = [e for e in all_eps if is_private_host(e[0])]
    endpoints = public_eps[:max(1, sample)]
    targets += [f"{h}:{p}" for h, p in endpoints]

    rows: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for item in targets:
        host, port = _split_host(item)
        if (host, port) in seen:
            continue
        seen.add((host, port))
        status, ms = tcp_probe(host, port, timeout)
        code = http_status(host, port, timeout + 1) if status == "ok" else None
        rows.append(_row(host, port, status, ms, code))

    return {
        "province": province,
        "isp": isp,
        "list": list_state,
        "endpoints": len(endpoints),
        "private_entries": len(private_eps),
        "rows": rows,
    }


def scan_province(province: str, isps=None, timeout: float = 4.0) -> dict:
    """扫一个省的几家运营商，看公开清单里哪家有。

    面板「扫本省 4 家」要的是「有没有、大约几条」，所以只拉清单数公网入口，
    不做 TCP 探测（那太慢）。四家并发拉，最慢也就一个 ``timeout``。
    """
    from concurrent.futures import ThreadPoolExecutor

    province = (province or "").strip().lower()
    if not province:
        return {"province": "", "isps": {}, "error": "缺少省份"}
    targets = list(isps) if isps else list(region.ISP_NAMES)

    def one(isp: str) -> tuple[str, dict]:
        text, state = fetch_list(province, isp, timeout=timeout)
        entries = len(parse_endpoints(text, include_private=False)) if state == "ok" else 0
        return isp, {"state": state, "entries": entries, "name": region.ISP_NAMES.get(isp, isp)}

    found: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(targets) or 1) as pool:
        for isp, info in pool.map(one, targets):
            found[isp] = info
    return {"province": province, "isps": found}


def _ipv6_note(ok_v6: bool) -> str:
    return "IPv6 可用" if ok_v6 else "没有 IPv6 出口"


def list_note(result: dict) -> str:
    """公开清单那一行的说明，单独抽出来是为了能测。"""
    state = result.get("list")
    if state == "missing":
        return "没有这一份（社区没整理过）"
    if state != "ok":
        return "取不到（网络问题，不影响结论）"
    if result.get("endpoints"):
        return f"取到 {result['endpoints']} 条可直连入口（抽样）"
    if result.get("private_entries"):
        return f"只有内网地址 {result['private_entries']} 条"
    return "文件在，但只有频道名、没有地址"


def verdict(info: dict, result: dict) -> list[str]:
    """把结果翻译成「能用 / 不能用 / 别白费劲」三档，外加下一步做什么。"""
    province = result.get("province") or ""
    isp = result.get("isp") or ""
    where = " · ".join(x for x in (region.PROVINCES.get(province, ""),
                                   region.ISP_NAMES.get(isp, "")) if x) or "这台机器的宽带"
    rows = result.get("rows") or []
    ok_rows = [r for r in rows if r["status"] == "ok"]
    lines: list[str] = []

    if ok_rows:
        lines.append(f"结论：{where}能连上运营商入口，这条路值得接。")
        lines.append("      自动按省取源（默认开）会自己把这份清单接进来，不用手工加源；")
        lines.append("      面板「运行设置」里可以关掉，湖北移动版已内置、不会重复接。")
        return lines

    if isp == "broadnet":
        lines.append(f"结论：{where}。广电宽带多数是租用别家出口，电视业务要么走同轴")
        lines.append("      （DVB-C），要么走 OTT 授权应用——这两种都没有可抓的 IP 单播源。")
        lines.append("      先看一眼机顶盒接的是网线还是同轴电缆：同轴就可以直接放弃这条路。")
        return lines

    if result.get("list") == "missing":
        lines.append(f"结论：{where}。公开清单里没有这个省份 + 运营商的组合，")
        lines.append("      说明社区里没人整理出可用入口——通常意味着对外没有统一单播地址。")
    elif result.get("private_entries"):
        lines.append(f"结论：{where}。公开清单里是运营商内网地址（10.x / 192.168.x），")
        lines.append("      只有在该省那条 IPTV 通道里才取得到，公网侧连不上是正常的。")
        lines.append("      想用得先把自己家的机顶盒接进来（见末尾）。")
    elif not rows:
        lines.append(f"结论：{where}。这一份只填了频道名、没填地址，等于没有，")
        lines.append("      要确认得从机顶盒那边动手（见末尾）。")
    else:
        if all(r["status"] == "dns" for r in rows):
            lines.append(f"结论：{where}。入口域名解析不了——可能是域名已废弃，")
            lines.append("      也可能这个入口只在运营商内网 DNS 里存在。")
        elif all(r["status"] in ("timeout", "refused") for r in rows):
            lines.append(f"结论：{where}。入口能解析但连不上——")
            if info.get("ipv6") is False:
                lines.append("      这台机器没有 IPv6 出口，而这类入口常常只下发 IPv6 边缘。")
            lines.append("      换成本省该运营商的宽带再试；不是的话就不是给你用的。")
        else:
            lines.append(f"结论：{where}。入口有响应但取不到清单，先按「没有」处理。")

    lines.append("")
    lines.append("要确认到底有没有，只能看自家机顶盒（只抓自己的设备）：")
    lines.append("  1. 机顶盒接网线 → 有 IP 通道；接同轴电缆 → 那是有线电视，没有 IP 源；")
    lines.append("  2. 抓开机时的 DHCP 和首个 HTTP 请求，能拿到 EPG / 认证地址就是单播；")
    lines.append("  3. 只抓到 IGMP 加入组播（239.x.x.x）→ 得先用 udpxy 转成 http 才能接。")
    return lines


def run(data_dir, *, manual_province: str = "", manual_isp: str = "",
        timeout: float = 4.0, echo=print) -> int:
    """``doctor --isp`` 的实现：检测 + 自测 + 打印结论。返回码 0 通 / 3 不通。"""
    info = region.detect(
        data_dir,
        manual_province=manual_province,
        manual_isp=manual_isp,
        force=True,
    )
    where = " · ".join(x for x in (info.get("province_name"), info.get("isp_name")) if x)
    echo("运营商自测（只读，不采集）")
    echo("")
    echo(f"本机宽带   {where or '没识别出来'} · {_ipv6_note(bool(info.get('ipv6')))}"
         + (f"    来源 {info.get('source')}" if info.get("source") else ""))
    if info.get("ip"):
        echo(f"出口地址   {info['ip']}")

    province, isp = info.get("province") or "", info.get("isp") or ""
    if not province and not isp:
        echo("")
        echo("认不出省份和运营商，测不了。可以在 config.toml 的 [output] 段写")
        echo('region = "hubei"、isp = "mobile" 手动指定后再跑一次。')
        return 3

    result = check(province, isp, timeout=timeout)
    echo("")
    if result["rows"]:
        echo("入口自测")
        rough = False
        for row in result["rows"]:
            addr = f"{row['host']}:{row['port']}"
            mark = {"ok": "通", "dns": "解析不了", "timeout": "超时",
                    "refused": "拒绝", "error": "失败"}.get(row["status"], row["status"])
            tail = f" · HTTP {row['code']}" if row.get("code") else ""
            if row.get("code") in (400, 404):
                rough = True
            rtt = f" · {row['ms']}ms" if row["ms"] else ""
            echo(f"  {addr:<42}{mark}{rtt}{tail}")
        if rough:
            echo("  （HTTP 400/404 是拿根路径试出来的，只说明端口通，不代表这条源能用）")
    else:
        echo("入口自测     没有可直连的入口可测")

    echo("")
    echo(f"公开清单      {list_note(result)}")
    echo("")
    for line in verdict(info, result):
        echo(line)

    return 0 if any(r["status"] == "ok" for r in result["rows"]) else 3


__all__ = [
    "KNOWN_HOSTS", "parse_endpoints", "is_private_host", "tcp_probe", "http_status",
    "fetch_list", "check", "scan_province", "list_note", "verdict", "run",
]
