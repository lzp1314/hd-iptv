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
from .pipeline import load_result, run_update
from .relay import Relay, RelayError
from .region import detect
from .settings import Settings, clean_secret, coerce_panel, write_panel

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


def _ysp_resolver(slug: str) -> tuple[str, str]:
    """中转层拿不到频道表之外的地址，这里的解析器只认 ysp 里的台。"""
    from . import ysp

    row = ysp.CHANNEL_BY_SLUG.get(slug.lower())
    if row is None:
        raise RelayError(f"未知频道: {slug}")
    url, _, _ = ysp.resolve(row[2], row[3], row[4], attempts=2)
    return url, ysp.UA


def live_target(raw_slug: str) -> tuple[str, str] | None:
    """判断 ``/live/<slug>.m3u8`` 里的 slug 该交给谁，返回 ``(key, 展示名)``。

    两套 slug 并存：

    - ``cctv1`` 这类：央视频的台，要现换票，地址不在 URL 里；
    - ``o:<base64>`` 这类：官方源（运营商 IPTV 等），上游地址整条编在 slug 里。

    只认前者的话，官方源接进来之后所有台都会回 404「未知频道」，播放器那边
    看到的就是「源服务器返回错误」——0.9.13 就是这么翻车的。播放器还会把
    ``o:`` 编码成 ``o%3A``，所以进来先还原。
    """
    from . import ysp
    from .relay import static_url

    key = unquote(raw_slug)
    row = ysp.CHANNEL_BY_SLUG.get(key.lower())
    if row is not None:
        return key.lower(), row[1]
    if static_url(key):
        return key, "运营商源"
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
        # 清单由本机取（用上游认的 UA），分片默认让播放器直连 CDN
        self.relay = Relay(
            _ysp_resolver,
            prefetch_every=settings.playlist_ttl,
            cache_bytes=int(settings.relay_cache_mb * 1024 * 1024),
            mode=settings.play_mode,
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
    def current_settings(self) -> dict:
        s = self.settings
        return {
            "interval_hours": s.interval_hours,
            "playlist_ttl": s.playlist_ttl,
            "cache_mb": int(s.relay_cache_mb),
            "play_mode": s.play_mode,
            "auto_region": s.auto_region,
            "region": s.region_hint,
            "isp": s.isp_hint,
            "public_url": s.public_url,
            # 只读：面板据此在每个地址后面带上密码段，前端不允许写回
            "secret": s.secret,
            "epg_url": s.epg_url,
            "epg_source": s.epg_source,
            "hidden": s.hidden,
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
        if "secret" in clean:
            clean["secret"] = clean_secret(clean["secret"])
        if clean:
            write_panel(self.settings.panel_path, clean)
            self.settings.panel.update(clean)
            self.relay.configure(
                prefetch_every=self.settings.playlist_ttl,
                cache_bytes=int(self.settings.relay_cache_mb * 1024 * 1024),
                mode=self.settings.play_mode,
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
    def trigger(self) -> bool:
        """异步触发一次更新，已有任务在跑时返回 False。"""
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.last_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            report = run_update(self.settings, log=self.log)
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
            try:
                text, stale, seq = state.relay.playlist_info(key, self._base())
            except RelayError as exc:
                self._json({"error": str(exc), "channel": name}, 502)
                return
            except Exception as exc:  # noqa: BLE001 - 失败要给出可读原因
                self._json({"error": f"{type(exc).__name__}: {exc}", "channel": name}, 502)
                return
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
                },
                cache="no-store",
            )

        def _playlist(self) -> None:
            """按请求现拼订阅列表。

            为什么不直接发 result.m3u：那份文件在更新时就把地址写死了，
            改了对外地址、套了反代都得等下一轮更新才生效。现拼的成本是
            几十个频道做一次字符串拼接，可以忽略。
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
            elif path in ("/txt", "/playlist.txt", "/result.txt"):
                result = load_result(settings) or {}
                channels = _visible(_channels_from_result(result), settings.hidden)
                if not channels:
                    self._json({"error": "结果尚未生成，请先更新一次"}, 404)
                    return
                text, _ = writers.render_txt(channels, relay_base=self._base(),
                                             lines_per_channel=settings.lines_per_channel)
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
                        "settings": state.current_settings(),
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
                self._json({"settings": state.current_settings(), "epg": state.epg.stats()})
            elif path == "/api/region/options":
                from .region import ISP_NAMES, PROVINCES

                self._json({
                    "provinces": [{"code": k, "name": v} for k, v in PROVINCES.items()],
                    "isps": [{"code": k, "name": v} for k, v in ISP_NAMES.items()],
                })
            elif path == "/api/channels":
                result = load_result(settings) or {}
                hidden = set(settings.hidden)
                rows = [
                    dict(row, hidden=row.get("name") in hidden)
                    for row in (result.get("channels", []) or [])
                ]
                self._json({
                    "generated_at": result.get("generated_at", ""),
                    "channels": rows,
                    # 面板要能列出「已隐藏」并提供一键恢复，所以整份返回、只打标记
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
                started = state.trigger()
                self._json({"started": started, "message": "已开始更新" if started else "已有任务在执行"})
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
                shown = {k: ("***" if k == "secret" and v else v) for k, v in updated.items()}
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
