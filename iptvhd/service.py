"""内置 HTTP 服务：结果分发 + 网页面板 + 定时更新。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .pipeline import load_result, run_update
from .settings import Settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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
        # 实时跳转用的地址缓存：slug -> (解析时间, 地址)
        self.live_cache: dict[str, tuple[float, str]] = {}

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
            """实时跳转：每次请求现场换一个可用的新地址。

            播放地址带时效，静态列表放久了会失效；用这个端点，播放器拿到的
            永远是刚换出来的地址。地址缓存 5 分钟，避免每次刷新都去打扰接口。
            """
            from . import ysp

            row = ysp.CHANNEL_BY_SLUG.get(slug.lower())
            if row is None:
                self._json({"error": f"未知频道: {slug}", "hint": "/api/live 可列出全部"}, 404)
                return

            cached = state.live_cache.get(row[0])
            if cached and time.time() - cached[0] < 300:
                self._redirect(cached[1])
                return

            try:
                url, _, _ = ysp.resolve(row[2], row[3], row[4], attempts=2)
            except Exception as exc:  # noqa: BLE001 - 失败要给出可读原因
                self._json({"error": str(exc), "channel": row[1]}, 502)
                return

            state.live_cache[row[0]] = (time.time(), url)
            self._redirect(url)

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
                        },
                    }
                )
            elif path == "/api/channels":
                result = load_result(settings) or {}
                self._json({"generated_at": result.get("generated_at", ""), "channels": result.get("channels", [])})
            elif path == "/health":
                self._json({"ok": True, "running": state.running})
            elif path.startswith(("/live/", "/ysp/")) and path.endswith(".m3u8"):
                slug = path.rsplit("/", 1)[-1][: -len(".m3u8")]
                self._live(slug)
            elif path == "/api/live":
                from . import ysp
                self._json({
                    "hint": "把 /live/<slug>.m3u8 填进播放器，每次请求都会换一个新地址",
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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        state.log("收到退出信号")
    finally:
        httpd.server_close()


__all__ = ["serve", "State", "make_handler"]
