"""极简 HTTP 客户端：只依赖标准库，但带上首字节延迟与吞吐计量。

探测直播流需要三样东西：HTTP 状态、内容类型、以及「下载 N 字节花了多久」。
urllib 本身不提供后两者的计时能力，所以这里自己用 http.client 实现。
"""

from __future__ import annotations

import gzip
import http.client
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from . import __version__

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_MAX_REDIRECTS = 5


class _V4HTTPSConnection(http.client.HTTPSConnection):
    """只走 IPv4 建连的 HTTPS 连接。

    ``socket.create_connection`` 默认按系统偏好选地址族，容器里常常优先 IPv6；
    某个上游把本机 IPv6 拉黑、IPv4 却正常时就会一直 403。这里强制用 AF_INET
    解析、逐地址尝试连接，但 ``host`` 仍是原域名——``http.client`` 会用它填
    Host 头和 TLS SNI，CDN 正是靠这两个字段分发，换成 IP 会拿到错误节点。
    """

    def connect(self) -> None:
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            raise OSError("IPv4 解析失败")
        last: OSError | None = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                break
            except OSError as exc:
                last = exc
                sock.close()
        else:
            raise last or OSError("IPv4 连接失败")
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        else:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _V4HTTPConnection(http.client.HTTPConnection):
    """只走 IPv4 建连的 HTTP 连接，理由同上。"""

    def connect(self) -> None:
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            raise OSError("IPv4 解析失败")
        last: OSError | None = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                break
            except OSError as exc:
                last = exc
                sock.close()
        else:
            raise last or OSError("IPv4 连接失败")
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        else:
            self.sock = sock


class _V6HTTPSConnection(http.client.HTTPSConnection):
    """只走 IPv6 建连的 HTTPS 连接，与 _V4 对称。

    运营商官方源的下发地址就是 ``[2409:...]:port`` 这类 IPv6 边缘，指定「仅 IPv6」
    时用它。host 仍是域名，Host 头与 SNI 都靠它，不能换成字面地址。
    """

    def connect(self) -> None:
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET6, socket.SOCK_STREAM)
        if not infos:
            raise OSError("IPv6 解析失败")
        last: OSError | None = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                break
            except OSError as exc:
                last = exc
                sock.close()
        else:
            raise last or OSError("IPv6 连接失败")
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        else:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _V6HTTPConnection(http.client.HTTPConnection):
    """只走 IPv6 建连的 HTTP 连接，理由同上。"""

    def connect(self) -> None:
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET6, socket.SOCK_STREAM)
        if not infos:
            raise OSError("IPv6 解析失败")
        last: OSError | None = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                break
            except OSError as exc:
                last = exc
                sock.close()
        else:
            raise last or OSError("IPv6 连接失败")
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        else:
            self.sock = sock


# ---- 域名解析的独立闸门 ----
# getaddrinfo 不受 socket 超时约束（它没有超时参数），解析器抽风时一个域名能
# 卡住几十秒。探测 332 条时只要有几条卡在解析上，整轮更新就跟着挂住——
# 这是「单条时限明明设了 15 秒，日志里却有 3 条跑了 90 秒」的来源。
_DNS_TTL = 600.0        # 解析成功的缓存时长
_DNS_FAIL_TTL = 120.0   # 解析失败的缓存时长（别反复撞同一个死域名）
_DNS_TIMEOUT = 4.0      # 单次解析的兜底时限
_DNS_MISS_TTL = 5.0     # 超时后的短暂「别再问了」窗口
_DNS_INFLIGHT = 32      # 同时最多挂多少个解析线程，超过就快速判超时

_dns_cache: dict[tuple[str, int], tuple[bool, float]] = {}
_dns_lock = threading.Lock()
_dns_inflight = 0
_dns_stats = {"timeouts": 0, "failures": 0, "ok": 0, "busy": 0}


def dns_stats() -> dict:
    """解析闸门的计数，给诊断接口用（超时/失败/成功/被并发上限挡掉）。"""
    with _dns_lock:
        return dict(_dns_stats, inflight=_dns_inflight)


def resolve(host: str, port: int, deadline: float | None = None) -> bool | None:
    """带时限的域名解析。

    返回 ``True`` 能解析、``False`` 解析失败、``None`` 超时（本次不等了）。

    **为什么每个解析单独开一个 daemon 线程，而不是共用一个线程池**：
    ``getaddrinfo`` 没有超时参数，遇到不回应的 DNS 就会一直挂着，而线程
    杀不掉。以前用固定 8 线程的池，只要凑够 8 个这样的解析，池就被占满，
    之后**所有**解析（包括央视频取票）都只能排队，4 秒到点报超时——现象
    是「系统里 nslookup 0.1 秒就出来，服务却一直说 DNS 解析超时」，而且
    进程不重启永远好不了。
    现在改成一条解析一个线程：卡住的那条只拖住它自己，别人照常解析。
    用 ``_DNS_INFLIGHT`` 兜住线程数，超了就按超时处理（不再堆线程）。
    """
    global _dns_inflight
    if not host:
        return True
    key = (host.lower(), int(port))
    now = time.monotonic()
    with _dns_lock:
        hit = _dns_cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        if _dns_inflight >= _DNS_INFLIGHT:
            _dns_stats["busy"] += 1
            return None
        _dns_inflight += 1

    budget = _DNS_TIMEOUT
    if deadline is not None:
        budget = max(0.2, min(_DNS_TIMEOUT, deadline - now))

    outcome: list[bool] = []
    done = threading.Event()

    def worker() -> None:
        global _dns_inflight
        try:
            socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            outcome.append(True)
        except OSError:
            outcome.append(False)
        except BaseException:  # noqa: BLE001 - 解析线程不许把异常漏出去
            outcome.append(False)
        finally:
            done.set()
            with _dns_lock:
                _dns_inflight -= 1

    threading.Thread(target=worker, name=f"dns-{host}"[:40], daemon=True).start()

    if not done.wait(budget):
        # 线程继续挂着（daemon，不挡进程退出），但我们不再等它。短暂记住
        # 这次失败，免得播放器的毫秒级重试把线程一条条堆起来。
        with _dns_lock:
            _dns_cache[key] = (False, time.monotonic() + _DNS_MISS_TTL)
            _dns_stats["timeouts"] += 1
        return None

    ok = outcome[0] if outcome else False
    with _dns_lock:
        _dns_cache[key] = (ok, time.monotonic() + (_DNS_TTL if ok else _DNS_FAIL_TTL))
        _dns_stats["ok" if ok else "failures"] += 1
    return ok


@dataclass(slots=True)
class Response:
    url: str = ""
    status: int = 0
    headers: dict = None
    body: bytes = b""
    ttfb_ms: int = 0        # 首字节延迟
    elapsed_ms: int = 0     # 首字节到读完的耗时
    error: str = ""
    # 读体被截止时间打断：body 不完整。有些源连得上、但一个字节一个字节往回滴，
    # 每次 read 都在超时内返回一点点，光靠 socket 超时永远判不出它已经废了。
    truncated: bool = False
    # 本次实际建连用的地址族（socket.AF_INET / AF_INET6 / 0=未知）。auto 模式据此
    # 判断 403 时该换哪一族——IPv6 被 CDN 拒、IPv4 正常时换对方向，只多打一枪。
    family: int = 0

    @property
    def content_type(self) -> str:
        if not self.headers:
            return ""
        return (self.headers.get("content-type") or "").lower()

    @property
    def size(self) -> int:
        return len(self.body)

    @property
    def speed_mbps(self) -> float:
        """按「传输阶段」计算速率，单位 Mbps。"""
        if self.elapsed_ms <= 0 or not self.body:
            return 0.0
        seconds = self.elapsed_ms / 1000.0
        return round(len(self.body) * 8 / seconds / 1_000_000, 2)

    @property
    def text(self) -> str:
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return ""


class HttpClient:
    """带重试、限长读取的轻量客户端。"""

    # 地址族三态：auto = 系统默认选路 + 403 时换族重试；ipv4/ipv6 = 只走该族。
    IP_MODES = ("auto", "ipv4", "ipv6")

    # 「哪个域名用哪一族」的记忆：域名 → (族, 到期时刻)。
    #
    # 为什么必须有：央视频的清单接口对出口 IPv4 单独限流（实测同一条清单地址
    # v4 一律 403、v6 一律 200）。没有这段记忆时，auto 每次请求都先撞一次 v4
    # 的 403 再换 v6 —— 每一次「成功」都先垫一个 403，几分钟就能把内部的出口
    # 冷却（连续 8 次 403 停 30 秒）顶出来，表现就是画面播一会儿卡一下。
    #
    # 记住之后，同一个域名下一次直接走能通的那一族，403 归零。TTL 到期自动失效，
    # 上游换策略时最多难受十分钟。
    _FAMILY_TTL = 600.0
    _FAMILY_PREF: dict[str, tuple[str, float]] = {}

    def __init__(
        self,
        user_agent: str = "",
        timeout: float = 8.0,
        retries: int = 1,
        proxy: str = "",
        force_ipv4: bool = False,
        ip_mode: str = "auto",
    ) -> None:
        self.user_agent = user_agent or DEFAULT_UA
        self.timeout = timeout
        self.retries = max(1, retries)
        self.proxy = proxy
        # force_ipv4 是旧接口，保留向后兼容：True 等价于 ip_mode="ipv4"。
        # 新代码一律用 ip_mode。三态默认 auto —— 老行为、老用户完全不变。
        mode = str(ip_mode or "auto").strip().lower()
        if force_ipv4:
            mode = "ipv4"
        self.ip_mode = mode if mode in self.IP_MODES else "auto"
        # 兼容旧读取点（tests / 老代码读 client.force_ipv4）
        self.force_ipv4 = self.ip_mode == "ipv4"
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------ #
    def get(
        self,
        url: str,
        *,
        limit: int = 0,
        headers: dict | None = None,
        allow_redirect: bool = True,
        deadline: float | None = None,
    ) -> Response:
        """读取 URL。limit > 0 时只读到 limit 字节就断开（用于测速）。

        ``deadline`` 是 ``time.monotonic()`` 口径的绝对截止时刻：连接、首字节、
        读体加起来不能超过它。慢滴流的源靠 socket 超时判不出来（每 8 秒挤一点，
        永远不超时），必须给它一个总时限。

        地址族策略（按顺序）：

        1. 这个域名记住了「哪一族能通」，且当前配置是 auto、或配置的那一族正好
           是记下来的坏族 → 直接走能通的那一族，不打无用的 403；
        2. 否则按配置走一次；
        3. 拿到 403（CDN 边缘拒绝，常见于某个出口地址被单独限流）→ 换另一族再试
           一次。auto 与强制单族都适用：强制族被出口级拒绝时，死磕只会一直拿不
           到数据，换族是唯一出路；
        4. 成功一次就把「能通的族」记下来（10 分钟），下一次不再先撞 403。
        """
        kw = dict(limit=limit, headers=headers, allow_redirect=allow_redirect, deadline=deadline)
        host = (urlsplit(url).hostname or "").lower()
        good, bad = ("", "") if self.proxy else self._family_memo(host)

        # 已知能通的族优先：auto 就别每次先撞一遍 403；强制单族时若那一族已被
        # 证明被拒，也不能一直撞。
        if good and (self.ip_mode == "auto" or self.ip_mode == bad):
            resp = self._get_loop(url, forced_mode=good, **kw)
            if resp.status and resp.status < 400 and resp.body:
                return resp
            # 记忆过期（上游换了策略）：清掉，按配置重新来一次
            self._family_memo_forget(host)

        last = self._get_loop(url, **kw)
        if last.status and last.status < 400 and last.body:
            self._family_remember(host, self._family_of(last.family), "")
            return last

        if last.status == 403 and not self.proxy:
            other = self._other_mode(last.family)
            if other:
                switched = self._get_loop(url, forced_mode=other, **kw)
                # 换族成功才采纳；换族仍失败就保留第一次的结果（保住原有错误文案）
                if switched.status and switched.status < 400 and switched.body:
                    self._family_remember(host, other, self._family_of(last.family))
                    return switched
        return last

    # ------------------------------------------------------------------ #
    # 地址族记忆：域名 → (能通的族, 被拒的族, 到期时刻)
    def _family_memo(self, host: str) -> tuple[str, str]:
        hit = self._FAMILY_PREF.get(host)
        if not hit:
            return "", ""
        good, bad, until = hit
        if time.monotonic() > until:
            self._FAMILY_PREF.pop(host, None)
            return "", ""
        return good, bad

    def _family_remember(self, host: str, good: str, bad: str) -> None:
        if not host or not good:
            return
        if len(self._FAMILY_PREF) > 256:
            self._FAMILY_PREF.clear()
        self._FAMILY_PREF[host] = (good, bad, time.monotonic() + self._FAMILY_TTL)

    def _family_memo_forget(self, host: str) -> None:
        self._FAMILY_PREF.pop(host, None)

    @staticmethod
    def _family_of(family: int) -> str:
        if family == socket.AF_INET:
            return "ipv4"
        if family == socket.AF_INET6:
            return "ipv6"
        return ""

    def _other_mode(self, family: int) -> str:
        """403 之后该换哪一族；没有可换的返回空串。"""
        if self.ip_mode == "ipv4":
            return "ipv6"
        if self.ip_mode == "ipv6":
            return "ipv4"
        known = self._family_of(family)
        if known == "ipv4":
            return "ipv6"
        if known == "ipv6":
            return "ipv4"
        # auto 且不知道实际用了哪一族：央视频的清单接口是 v4 被拒、v6 正常，
        # 先试 v6 更可能一击命中。
        return "ipv6"

    def _get_loop(
        self,
        url: str,
        *,
        limit: int,
        headers: dict | None,
        allow_redirect: bool,
        deadline: float | None,
        forced_mode: str = "",
    ) -> Response:
        """单次地址族策略下的重试循环。forced_mode 非空时覆盖 self.ip_mode。"""
        last = Response(url=url, error="未执行")
        for attempt in range(self.retries):
            last = self._get_once(
                url,
                limit=limit,
                headers=headers,
                allow_redirect=allow_redirect,
                deadline=deadline,
                forced_mode=forced_mode,
            )
            if last.truncated:
                return last  # 慢源不重试：重试只是把同样的等待再做一遍
            if last.status and last.status < 400 and last.body:
                return last
            if last.status in (401, 403, 404, 410):
                return last  # 明确的拒绝不必重试
            if deadline is not None and time.monotonic() >= deadline:
                return last  # 已经到点，别把重试的时间也搭进去
            if attempt + 1 < self.retries:
                time.sleep(0.4 * (attempt + 1))
        return last

    # ------------------------------------------------------------------ #
    def _op_timeout(self, deadline: float | None) -> float:
        """单次 socket 操作的超时：不超过配置值，也不越过截止时刻。"""
        if deadline is None:
            return self.timeout
        left = deadline - time.monotonic()
        return max(0.05, min(self.timeout, left))

    # ------------------------------------------------------------------ #
    def _get_once(
        self,
        url: str,
        *,
        limit: int,
        headers: dict | None,
        allow_redirect: bool,
        deadline: float | None = None,
        forced_mode: str = "",
    ) -> Response:
        resp = Response(url=url)
        current = url
        for _ in range(_MAX_REDIRECTS if allow_redirect else 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https"):
                resp.error = f"不支持的协议: {parts.scheme}"
                return resp

            hdrs = {
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            if headers:
                hdrs.update(headers)
            if limit > 0:
                hdrs["Range"] = f"bytes=0-{limit - 1}"

            port = parts.port or (443 if parts.scheme == "https" else 80)
            state = resolve(parts.hostname or "", port, deadline)
            if state is not True:
                # 解析不出地址就没必要再走 socket：超时的不等、失败的也不重试
                resp.error = "DNS 解析超时" if state is None else "域名解析失败"
                resp.truncated = True
                return resp

            started = time.monotonic()
            try:
                conn = self._connect(parts, deadline, forced_mode=forced_mode)
                if self.proxy and parts.scheme == "http":
                    target = current  # 走代理时 http 请求目标用绝对地址
                else:
                    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
                conn.request("GET", target, headers=hdrs)
                # 记录本次实际建连的地址族，供 auto 模式判断 403 时换哪一族。
                # http.client 是懒连接：sock 在 request() 触发 connect 后才有。
                if conn.sock is not None:
                    resp.family = conn.sock.family
                raw = conn.getresponse()

                resp.status = raw.status
                resp.headers = {k.lower(): v for k, v in raw.getheaders()}
                resp.url = current
                resp.ttfb_ms = int((time.monotonic() - started) * 1000)

                location = resp.headers.get("location")
                if raw.status in (301, 302, 303, 307, 308) and location and allow_redirect:
                    raw.read(0)
                    conn.close()
                    current = _join(current, location)
                    continue

                # 首字节回来可能已经用掉大半个截止时间，读体按剩下的时间收紧
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(self._op_timeout(deadline))
                    except OSError:
                        pass
                body_started = time.monotonic()
                resp.body, resp.truncated = self._read_body(raw, limit, deadline)
                resp.elapsed_ms = int((time.monotonic() - body_started) * 1000)
                if resp.truncated:
                    resp.error = "读取超时"
                conn.close()
                return self._maybe_inflate(resp)

            except Exception as exc:  # noqa: BLE001 - 网络异常种类繁多
                resp.error = f"{type(exc).__name__}: {exc}"
                resp.status = resp.status or 0
                resp.truncated = resp.truncated or (
                    deadline is not None and time.monotonic() >= deadline
                )
                return resp

        resp.error = resp.error or "重定向次数过多"
        return resp

    # ------------------------------------------------------------------ #
    def _connect(self, parts, deadline: float | None = None, forced_mode: str = ""):
        timeout = self._op_timeout(deadline)
        proxy_host, _, proxy_port = self.proxy.partition(":")
        use_proxy = bool(self.proxy)
        # 走代理时代理自己选路，地址族开关无效；forced_mode 是 auto 换族重试的临时覆盖。
        mode = forced_mode or self.ip_mode

        if parts.scheme == "https":
            if use_proxy:
                conn = http.client.HTTPSConnection(
                    proxy_host, int(proxy_port or 8080), timeout=timeout, context=self._ssl
                )
                conn.set_tunnel(parts.hostname, parts.port or 443)
                return conn
            cls = {
                "ipv4": _V4HTTPSConnection,
                "ipv6": _V6HTTPSConnection,
            }.get(mode, http.client.HTTPSConnection)
            return cls(parts.hostname, parts.port or 443, timeout=timeout, context=self._ssl)
        if use_proxy:
            return http.client.HTTPConnection(proxy_host, int(proxy_port or 8080), timeout=timeout)
        cls = {
            "ipv4": _V4HTTPConnection,
            "ipv6": _V6HTTPConnection,
        }.get(mode, http.client.HTTPConnection)
        return cls(parts.hostname, parts.port or 80, timeout=timeout)

    @staticmethod
    def _read_body(raw, limit: int, deadline: float | None = None) -> tuple[bytes, bool]:
        """读响应体，返回 ``(内容, 是否被截止时间截断)``。

        只按字节数停是不够的：慢滴流每次 read 都能拿到一点，永远到不了 limit。
        所以每轮都看一眼总时间，到点就走人。
        """
        chunk = 16384
        buf = bytearray()
        while True:
            if limit > 0 and len(buf) >= limit:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return bytes(buf), True
            try:
                piece = raw.read(chunk if limit <= 0 else min(chunk, limit - len(buf)))
            except Exception:  # noqa: BLE001 - 直播流常在读中途被截断
                break
            if not piece:
                break
            buf.extend(piece)
        if deadline is not None and time.monotonic() >= deadline:
            return bytes(buf), True
        return bytes(buf), False

    @staticmethod
    def _maybe_inflate(resp: Response) -> Response:
        encoding = (resp.headers or {}).get("content-encoding", "").lower()
        if encoding == "gzip" and resp.body:
            try:
                resp.body = gzip.decompress(resp.body)
            except Exception:  # noqa: BLE001
                pass
        return resp


def _join(base: str, location: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, location)


def build_client(
    user_agent: str = "",
    timeout: float = 8.0,
    retries: int = 1,
    proxy: str = "",
    ip_mode: str = "auto",
) -> HttpClient:
    return HttpClient(
        user_agent=user_agent, timeout=timeout, retries=retries, proxy=proxy, ip_mode=ip_mode
    )


__all__ = ["HttpClient", "Response", "build_client", "resolve", "DEFAULT_UA", "__version__"]
