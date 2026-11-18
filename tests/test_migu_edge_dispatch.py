"""咪咕第三条路：服务端解析一次 302、真实节点交给播放器直连。

起真服务打真请求验证 /live 的行为：开关打开时回 302 到真实节点地址（而不是
本机 /relay/），解析失败回落到 relay，关掉开关走旧的 client_dispatch。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from iptvhd import service
from iptvhd.service import State, make_handler
from iptvhd.settings import load_settings

RESULT = {"generated_at": "2026-11-18 20:00:00", "channels": []}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestMiguEdgeDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "output").mkdir()
        (root / "output" / "latest.json").write_text(json.dumps(RESULT), encoding="utf-8")
        os.environ.pop("HDIPTV_SECRET", None)

        self.settings = load_settings(root)
        self.state = State(self.settings)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.tmp.cleanup)
        self.port = self.httpd.server_address[1]

    def _get_no_follow(self, path: str):
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers

    def test_edge_dispatch_redirects_to_real_node(self):
        """默认路径：回 302，Location 是真实节点地址。"""
        node = "http://edge.example.com/live/ch/index.m3u8?token=abc"
        with patch.object(service, "migu_edge_url", return_value=node) as fake:
            code, headers = self._get_no_follow("/live/m:608807420.m3u8")
        self.assertIn(code, (301, 302))
        self.assertEqual(headers.get("Location"), node)
        fake.assert_called_once()

    def test_edge_failure_falls_back_to_relay(self):
        """解析不出节点时不回 302 到咪咕，而是回落 relay（返回清单或错误 JSON）。"""
        with patch.object(service, "migu_edge_url", return_value=""), \
             patch.object(self.state.relay, "playlist_info", side_effect=service.RelayError("上游不可用")):
            code, headers = self._get_no_follow("/live/m:608807420.m3u8")
        # 没有 Location 指向咪咕节点（回落了 relay，relay 失败给 502 JSON）
        self.assertEqual(code, 502)
        self.assertIsNone(headers.get("Location"))

    def test_edge_off_uses_old_client_dispatch(self):
        """关掉 edge、打开旧 client_dispatch：回 302 到未跳转的调度地址。"""
        self.settings.panel["migu_edge_dispatch"] = False
        self.settings.panel["migu_client_dispatch"] = True
        dispatch = "http://gslb.example.com/dispatch?sign=x"
        with patch.object(service, "migu_ticket", return_value=dispatch) as fake_ticket, \
             patch.object(service, "migu_edge_url") as fake_edge:
            code, headers = self._get_no_follow("/live/m:608807420.m3u8")
        self.assertIn(code, (301, 302))
        self.assertEqual(headers.get("Location"), dispatch)
        fake_edge.assert_not_called()
        fake_ticket.assert_called_once()


if __name__ == "__main__":
    unittest.main()
