"""端到端测试：起一个本地 HLS 夹具服务器，跑完整管线。

覆盖：订阅采集 → 频道匹配 → 主列表解析 → 分片测速 → 分辨率过滤 → 输出。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from iptvhd.pipeline import run_update
from iptvhd.settings import load_settings

SEG = b"\x47" * 400_000  # 伪造的 TS 分片，够大才好测速

MASTER_1080 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",FRAME-RATE=25
/1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360
/360/index.m3u8
"""

MASTER_360 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
/360/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXTINF:6.006,
seg1.ts
#EXTINF:6.006,
seg2.ts
"""


class _FixtureServer:
    """极简 HTTP 服务，支持 Range，用于模拟真实直播源。"""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[bytes, str]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                item = outer.routes.get(path)
                if item is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, ctype = item
                start = 0
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    try:
                        start = int(rng[6:].split("-")[0])
                    except ValueError:
                        start = 0
                payload = body[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except Exception:  # noqa: BLE001 - 客户端提前断开是正常的
                    pass

            def log_message(self, fmt, *args):  # 静音
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class TestPipelineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _FixtureServer()
        base = cls.fixture.base
        cls.fixture.routes.update(
            {
                "/list.m3u": (
                    (
                        "#EXTM3U\n"
                        '#EXTINF:-1 tvg-name="演示A" group-title="演示",演示A 高清\n'
                        f"{base}/1080/master.m3u8\n"
                        '#EXTINF:-1 tvg-name="演示B" group-title="演示",演示B\n'
                        f"{base}/sd/master.m3u8\n"
                        '#EXTINF:-1 tvg-name="演示C" group-title="演示",演示C\n'
                        f"{base}/broken.m3u8\n"
                    ).encode(),
                    "audio/x-mpegurl",
                ),
                "/1080/master.m3u8": (MASTER_1080.encode(), "application/vnd.apple.mpegurl"),
                "/sd/master.m3u8": (MASTER_360.encode(), "application/vnd.apple.mpegurl"),
                "/1080/index.m3u8": (MEDIA.encode(), "application/vnd.apple.mpegurl"),
                "/360/index.m3u8": (MEDIA.encode(), "application/vnd.apple.mpegurl"),
                "/1080/seg1.ts": (SEG, "video/mp2t"),
                "/1080/seg2.ts": (SEG, "video/mp2t"),
                "/360/seg1.ts": (SEG, "video/mp2t"),
                "/360/seg2.ts": (SEG, "video/mp2t"),
            }
        )
        cls.fixture.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.stop()

    def _make_project(self, root: Path) -> None:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "config.toml").write_text(
            """
[app]
data_dir = "output"

[update]
interval_hours = 0
run_on_start = false

[fetch]
timeout = 5
retries = 1

[probe]
enabled = true
concurrency = 2
sample_bytes = 262144
min_speed_mbps = 0.01
min_height = 720
max_per_channel = 3
""",
            encoding="utf-8",
        )
        (root / "config" / "channels.txt").write_text(
            "演示,#genre#\n演示A\n演示B\n演示C\n", encoding="utf-8"
        )
        (root / "config" / "sources.toml").write_text(
            f"""
[[source]]
name = "fixture"
type = "subscription"
url = "{self.fixture.base}/list.m3u"
enabled = true
""",
            encoding="utf-8",
        )

    def test_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            settings = load_settings(root)
            logs: list[str] = []
            report = run_update(settings, log=logs.append)

            self.assertEqual(report.collected, 3)
            self.assertEqual(report.probed, 3)
            self.assertEqual(report.alive, 1, "只有 1080P 那条应该通过；360P 与 404 应被过滤")
            self.assertEqual(report.published, 1)

            m3u = (root / "output" / "result.m3u").read_text(encoding="utf-8")
            self.assertIn("/1080/master.m3u8", m3u)
            self.assertNotIn("/sd/master.m3u8", m3u)
            self.assertIn("tvg-name=\"演示A\"", m3u)

            txt = (root / "output" / "result.txt").read_text(encoding="utf-8")
            self.assertIn("演示,#genre#", txt)
            self.assertIn("/1080/master.m3u8", txt)

            # 分辨率来自主播放列表的 RESOLUTION，不是靠猜
            self.assertTrue(any("1080" in line for line in logs))

    def test_allow_unknown_keeps_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project(root)
            cfg = (root / "config" / "config.toml").read_text(encoding="utf-8")
            (root / "config" / "config.toml").write_text(
                cfg.replace("min_height = 720", "min_height = 720\nallow_unknown_resolution = true"),
                encoding="utf-8",
            )
            settings = load_settings(root)
            report = run_update(settings, log=lambda _m: None)
            self.assertEqual(report.published, 1)


if __name__ == "__main__":
    unittest.main()
