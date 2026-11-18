"""「我的频道」(/api/channels) 与订阅共用同一套模块开关过滤。

以前的 bug：订阅按开关过滤了、面板没过滤，导致关掉的源还赖在「我的频道」
列表里（用户只勾了央视频，列表却还显示运营商官方源/第三方聚合源）。这里
起真服务打真请求锁住两者一致，并覆盖"关的是底层源名"这种旧配置。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from iptvhd.service import State, make_handler
from iptvhd.settings import load_settings

# 三个模块各一条频道；group 是 pipeline 归一过的展示模块名
RESULT = {
    "generated_at": "2026-11-18 20:00:00",
    "channels": [
        {"name": "央视一套", "group": "央视频",
         "streams": [{"slug": "ysp1", "url": "http://up/a.m3u8", "height": 1080}]},
        {"name": "CCTV-1", "group": "运营商官方源",
         "streams": [{"slug": "c1", "url": "http://up/b.m3u8", "height": 1080}]},
        {"name": "凤凰卫视", "group": "第三方聚合源",
         "streams": [{"slug": "ph", "url": "http://up/c.m3u8", "height": 1080}]},
    ],
}


class TestChannelListModuleFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "output").mkdir()
        (root / "output" / "latest.json").write_text(json.dumps(RESULT), encoding="utf-8")
        (root / "output" / "result.json").write_text(json.dumps(RESULT), encoding="utf-8")
        os.environ.pop("HDIPTV_SECRET", None)
        self.addCleanup(os.environ.pop, "HDIPTV_SECRET", None)
        self.settings = load_settings(root)
        self.state = State(self.settings)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.addCleanup(self.httpd.server_close)
        self.thread = __import__("threading").Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _channels(self) -> list[dict]:
        with urllib.request.urlopen(f"{self.base}/api/channels", timeout=5) as r:
            return json.loads(r.read().decode())["channels"]

    def _write_disabled(self, names: list[str]) -> None:
        (self.settings.panel_path).write_text(
            json.dumps({"disabled_modules": names}), encoding="utf-8"
        )
        self.settings.panel = __import__("iptvhd.settings", fromlist=["coerce_panel"]).coerce_panel(
            {"disabled_modules": names}
        )

    def test_no_disabled_returns_all(self):
        self.assertEqual(len(self._channels()), 3)

    def test_disable_one_module_hides_its_channels(self):
        """只勾央视频（关掉另外两个）：面板只剩央视频，和订阅一致。"""
        self._write_disabled(["运营商官方源", "第三方聚合源"])
        rows = self._channels()
        self.assertEqual([r["name"] for r in rows], ["央视一套"])
        self.assertEqual([r["group"] for r in rows], ["央视频"])

    def test_disable_display_name_hides_whole_module(self):
        """关展示名「央视频」应把它两个底层源（综合频道/央视官方）的台都摘掉。"""
        self._write_disabled(["央视频"])
        self.assertEqual(sorted(r["name"] for r in self._channels()),
                         ["CCTV-1", "凤凰卫视"])

    def test_disable_underlying_source_name_also_works(self):
        """旧配置关的是底层源名「官方直连」，归一到运营商官方源后同样生效。"""
        self._write_disabled(["官方直连", "聚合源-多线路"])
        self.assertEqual([r["name"] for r in self._channels()], ["央视一套"])

    def test_hidden_flag_still_works_with_module_filter(self):
        """模块过滤和隐藏标记叠加：关掉的台不出现，留下的台隐藏标记仍正确。"""
        self._write_disabled(["第三方聚合源"])
        self.settings.panel = __import__("iptvhd.settings", fromlist=["coerce_panel"]).coerce_panel(
            {"disabled_modules": ["第三方聚合源"], "hidden": ["央视一套"]}
        )
        rows = self._channels()
        self.assertEqual([r["name"] for r in rows], ["央视一套", "CCTV-1"])
        by_name = {r["name"]: r for r in rows}
        self.assertTrue(by_name["央视一套"]["hidden"])
        self.assertFalse(by_name["CCTV-1"]["hidden"])


if __name__ == "__main__":
    unittest.main()
