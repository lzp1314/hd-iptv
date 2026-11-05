"""内置 HTTP 服务：结果分发 + 网页面板 + 定时更新。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .pipeline import load_result, run_update
from .relay import Relay, RelayError
from .settings import Settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 中转层拿不到频道表之外的地址，这里的解析器只认 ysp 里的台
def _ysp_resolver(slug: str) -> tuple[str, str]:
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
        self.relay = Relay(_ysp_resolver)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-200:]

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


def make_handler(state: State):
    settings = state.settings

    class Handler(BaseHTTPRequestHandler):
        server_version = f"hd-iptv/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---------------- 基础工具 ----------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _file(self, path: Path, ctype: str, download: str = "") -> None:
            if not path.exists():
                self._json({"error": "结果尚未生成，请先更新一次", "path": str(path)}, 404)
                return
            extra = {"Content-Disposition": f'inline; filename="{download}"'} if download else None
            self._send(200, path.read_bytes(), ctype, extra)

        def _redirect(self, url: str) -> None:
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

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
                text = state.relay.playlist(key)
            except RelayError as exc:
                self._json({"error": str(exc), "channel": row[1]}, 502)
                return
            except Exception as exc:  # noqa: BLE001 - 失败要给出可读原因
                self._json({"error": f"{type(exc).__name__}: {exc}", "channel": row[1]}, 502)
                return
            self._send(200, text.encode("utf-8"), "application/vnd.apple.mpegurl; charset=utf-8")

        def _relay(self, path: str) -> None:
            """/relay/<slug>/p/<token>：转发子清单、分片和密钥。"""
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[0] != "relay" or parts[2] != "p":
                self._json({"error": "地址格式应为 /relay/<slug>/p/<token>"}, 400)
                return
            try:
                ctype, body = state.relay.resource(parts[1], parts[3])
            except RelayError as exc:
                self._json({"error": str(exc)}, 502)
                return
            self._send(200, body, ctype)

        def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷屏
            return

        # ---------------- 路由 ----------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            data_dir = settings.data_dir

            if path in ("/", "/index.html"):
                self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/static/app.js":
                self._file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            elif path == "/static/style.css":
                self._file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            elif path in ("/m3u", "/m3u8", "/result.m3u"):
                self._file(data_dir / "result.m3u", "audio/x-mpegurl; charset=utf-8", "result.m3u")
            elif path == "/txt":
                self._file(data_dir / "result.txt", "text/plain; charset=utf-8", "result.txt")
            elif path == "/json":
                self._file(data_dir / "result.json", "application/json; charset=utf-8")
            elif path == "/api/status":
                result = load_result(settings) or {}
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
                        "endpoints": {
                            "m3u": "/m3u",
                            "txt": "/txt",
                            "json": "/json",
                            "live": "/live/<slug>.m3u8",
                            "live_list": "/api/live",
                            "relay": "/relay/<slug>/p/<token>",
                            "version": "/api/version?check=1",
                        },
                    }
                )
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
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def _scheduler(state: State, interval_hours: float) -> None:
    interval = max(0.05, interval_hours) * 3600
    while True:
        try:
            state.trigger()
        except Exception as exc:  # noqa: BLE001
            state.log(f"! 定时任务异常：{exc}")
        time.sleep(interval)


def serve(settings: Settings, host: str = "0.0.0.0", port: int = 9797) -> None:
    state = State(settings)
    report = load_result(settings).get("report", {})
    if report:
        state.report = report
        state.last_finished = report.get("finished_at", "")

    if settings.run_on_start:
        state.trigger()
    interval = settings.interval_hours
    if interval > 0:
        threading.Thread(target=_scheduler, args=(state, interval), daemon=True).start()
        state.log(f"定时更新已启用：每 {interval} 小时一次")

    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    state.log(f"服务已启动：http://{host}:{port}  （订阅地址 /m3u）")
    if not settings.public_url:
        state.log("提示：未设置 PUBLIC_URL，列表里会给上游直连地址；"
                  "播放器若不认 UA 会卡住，建议设成 http://<本机IP>:%d" % port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        state.log("收到退出信号")
    finally:
        httpd.server_close()


__all__ = ["serve", "State", "make_handler"]
