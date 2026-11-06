"""内置 HTTP 服务：结果分发 + 网页面板 + 定时更新。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, writers
from .epg import Epg
from .logos import Logos
from .models import Channel, Stream
from .pipeline import load_result, run_update
from .relay import Relay, RelayError
from .settings import Settings, coerce_panel, write_panel

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

M3U_TYPE = "audio/x-mpegurl; charset=utf-8"


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
            if not url:
                continue
            streams.append(
                Stream(
                    url=url,
                    slug=str(item.get("slug") or ""),
                    height=int(item.get("height") or 0),
                    width=int(item.get("width") or 0),
                )
            )
        name = str(row.get("name") or "")
        if name and streams:
            channels.append(Channel(name=name, group=str(row.get("group") or "未分组"), streams=streams))
    return channels


def _ysp_resolver(slug: str) -> tuple[str, str]:
    """中转层拿不到频道表之外的地址，这里的解析器只认 ysp 里的台。"""
    from . import ysp

    row = ysp.CHANNEL_BY_SLUG.get(slug.lower())
    if row is None:
        raise RelayError(f"未知频道: {slug}")
    url, _, _ = ysp.resolve(row[2], row[3], row[4], attempts=2)
    return url, ysp.UA


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
        # 清单与分片的中转：播放器只跟本机说话，UA 由服务端代劳
        self.relay = Relay(
            _ysp_resolver,
            playlist_ttl=settings.playlist_ttl,
            cache_bytes=int(settings.relay_cache_mb * 1024 * 1024),
        )
        self.epg = Epg(settings.data_dir, settings.epg_source, settings.epg_ttl_hours)
        self.logos = Logos(settings.data_dir / "logos")

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-200:]

    # ---------------- 设置 ----------------
    def current_settings(self) -> dict:
        s = self.settings
        return {
            "interval_hours": s.interval_hours,
            "playlist_ttl": s.playlist_ttl,
            "cache_mb": int(s.relay_cache_mb),
            "public_url": s.public_url,
            "epg_url": s.epg_url,
            "epg_source": s.epg_source,
        }

    def apply_settings(self, values: dict) -> dict:
        """面板提交的设置：落盘 + 当场生效，不用重启容器。"""
        clean = coerce_panel(values)
        if clean:
            write_panel(self.settings.panel_path, clean)
            self.settings.panel.update(clean)
            self.relay.configure(
                playlist_ttl=self.settings.playlist_ttl,
                cache_bytes=int(self.settings.relay_cache_mb * 1024 * 1024),
            )
            if "epg_source" in clean:
                # 换了节目单源就把旧的清掉，免得播放器拿到对不上的节目单
                self.epg.source = self.settings.epg_source
                self.epg.clear()
                names = self._channel_names()
                if self.epg.source and names:
                    threading.Thread(target=self._warm_epg, args=(names,), daemon=True).start()
        return self.current_settings()

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
            """拼对外地址。

            配了 ``PUBLIC_URL`` 就用它；没配就按这次请求的 Host 现算——
            套了一层反向代理（Lucky / Nginx）时，``X-Forwarded-*`` 会带出
            真实的外网域名和端口，生成的播放地址自然就是外网能用的。
            """
            public = settings.public_url
            if public:
                return public.rstrip("/")
            host = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
            host = host or (self.headers.get("Host") or "").strip()
            if not host:
                return ""
            proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            if proto not in ("http", "https"):
                proto = "https" if self.headers.get("X-Forwarded-Ssl") == "on" else "http"
            return f"{proto}://{host}"

        # ---------------- 播放 ----------------
        def _live(self, slug: str) -> None:
            """返回改写后的播放清单，分片同样走本机转发。

            早先这里是 302 跳到上游地址，但跳转只能换地址、换不了 User-Agent：
            播放器取 HLS 子清单和分片时用的仍是自己的 UA，照样被上游拒掉，
            现象就是列表里只有一个台能放。改成整条链路本机转发之后，
            播放器用什么 UA 都无所谓了。
            """
            from . import ysp

            key = slug.lower()
            row = ysp.CHANNEL_BY_SLUG.get(key)
            if row is None:
                self._json({"error": f"未知频道: {slug}", "hint": "/api/live 可列出全部"}, 404)
                return
            try:
                text = state.relay.playlist(key, self._base())
            except RelayError as exc:
                self._json({"error": str(exc), "channel": row[1]}, 502)
                return
            except Exception as exc:  # noqa: BLE001 - 失败要给出可读原因
                self._json({"error": f"{type(exc).__name__}: {exc}", "channel": row[1]}, 502)
                return
            self._send(200, text.encode("utf-8"), "application/vnd.apple.mpegurl; charset=utf-8")

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

            base = self._base()
            logo_base = settings.logo_base or (f"{base}/logo" if base else "")
            epg_url = settings.epg_url or (f"{base}/epg.xml" if base else "")
            text, _ = writers.render_m3u(
                channels,
                epg_url=epg_url,
                logo_base=logo_base,
                relay_base=base,
                tvg_ids=state.epg.mapping(),
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
            try:
                code, ctype, body, extra = state.relay.resource(
                    parts[1], parts[3], self.headers.get("Range", ""), self._base()
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

        def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷屏
            return

        # ---------------- 路由 ----------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path

            if path in ("/", "/index.html"):
                self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/static/app.js":
                self._file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            elif path == "/static/style.css":
                self._file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            elif path in ("/m3u", "/m3u8", "/playlist.m3u", "/list.m3u", "/result.m3u"):
                # .m3u8 是 HLS 媒体清单的扩展名，播放器会当成「单个直播流」解析，
                # 然后报「播放格式不受支持」——所以这个入口改成跳转到 .m3u
                if path == "/m3u8":
                    self._redirect("/playlist.m3u")
                else:
                    self._playlist()
            elif path in ("/txt", "/playlist.txt", "/result.txt"):
                result = load_result(settings) or {}
                channels = _channels_from_result(result)
                if not channels:
                    self._json({"error": "结果尚未生成，请先更新一次"}, 404)
                    return
                text, _ = writers.render_txt(channels, relay_base=self._base())
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            elif path in ("/json", "/result.json"):
                self._file(settings.data_dir / "result.json", "application/json; charset=utf-8")
            elif path == "/epg.xml":
                self._epg()
            elif path.startswith("/logo/"):
                self._logo(path)
            elif path == "/api/status":
                result = load_result(settings) or {}
                relay_stats = state.relay.stats()
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
                        "epg": state.epg.stats(),
                        "logos": state.logos.stats(),
                        # 上游被限流时面板要能说清楚，而不是只让用户看到画面卡住
                        "relay": {
                            "failures": relay_stats["failures"],
                            "stale_served": relay_stats["stale_served"],
                            "plays": len(relay_stats["cached"]),
                            "errors": list(relay_stats["errors"].values())[:3],
                            "segments": relay_stats["segments"],
                        },
                        "endpoints": {
                            "m3u": "/playlist.m3u",
                            "txt": "/txt",
                            "json": "/json",
                            "epg": "/epg.xml",
                            "logo": "/logo/<频道名>.png",
                            "live": "/live/<slug>.m3u8",
                            "live_list": "/api/live",
                            "relay": "/relay/<slug>/p/<token>",
                            "settings": "/api/settings",
                            "version": "/api/version?check=1",
                        },
                    }
                )
            elif path == "/api/settings":
                self._json({"settings": state.current_settings(), "epg": state.epg.stats()})
            elif path == "/api/channels":
                result = load_result(settings) or {}
                self._json({"generated_at": result.get("generated_at", ""), "channels": result.get("channels", [])})
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
                self._json({
                    "hint": "把 /live/<slug>.m3u8 填进播放器，由本机转发，任何播放器都能放",
                    "channels": [{"slug": r[0], "name": r[1], "url": f"/live/{r[0]}.m3u8"}
                                 for r in ysp.CHANNELS],
                })
            else:
                self._json({"error": "not found", "path": path}, 404)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/api/update":
                started = state.trigger()
                self._json({"started": started, "message": "已开始更新" if started else "已有任务在执行"})
            elif path == "/api/settings":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(min(length, 64 * 1024)) if length else b"{}"
                    values = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(values, dict):
                        raise ValueError("需要一个 JSON 对象")
                except (ValueError, UnicodeDecodeError) as exc:
                    self._json({"error": f"请求体不是合法 JSON：{exc}"}, 400)
                    return
                updated = state.apply_settings(values)
                state.log(f"[设置] 已更新 {', '.join(f'{k}={v}' for k, v in updated.items())}")
                self._json({"ok": True, "settings": updated})
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
    interval = settings.interval_hours
    state.log(
        f"定时更新：每 {interval:g} 小时一次" if interval > 0 else "定时更新：已关闭（面板里可以打开）"
    )

    httpd = Server((host, port), make_handler(state))
    state.log(f"服务已启动：http://{host}:{port}  （订阅地址 /playlist.m3u）")
    state.log(
        f"分片缓存上限 {int(settings.relay_cache_mb)} MB，清单缓存 {settings.playlist_ttl:g} 秒，"
        "多客户端共用一个上游连接池"
    )
    if not settings.public_url:
        state.log("提示：未设置对外地址，列表按请求的 Host 现拼；套了反代会自动用外网域名")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        state.log("收到退出信号")
    finally:
        httpd.server_close()


__all__ = ["serve", "State", "make_handler"]
