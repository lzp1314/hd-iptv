"""访问密码：URL 里必须带一段路径，不带就进不来。

这类改动最容易出的问题是「保护了首页，忘了订阅」或者「清单里的分片地址没带
密码段」——播放器能拉到列表、一播就 401。所以这里直接起一个真服务打真请求，
把面板、订阅、静态资源、接口、健康检查全过一遍。
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

from iptvhd.service import State, make_handler, secret_prefix, with_secret
from iptvhd.settings import load_settings

SECRET = "tv7Kd2x"

RESULT = {
    "generated_at": "2026-11-10 20:00:00",
    "channels": [
        {
            "name": "CCTV-1",
            "group": "央视",
            "streams": [{"url": "http://up.example.com/1.m3u8", "slug": "cctv1", "height": 1080}],
        }
    ],
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestSecretHelpers(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(secret_prefix(""), "")
        self.assertEqual(secret_prefix("abc"), "/abc")
        self.assertEqual(secret_prefix("a b"), "/a%20b")

    def test_with_secret_appends_once(self):
        self.assertEqual(with_secret("http://h:9797", "abc"), "http://h:9797/abc")
        self.assertEqual(with_secret("http://h:9797/", "abc"), "http://h:9797/abc")
        # 用户自己把密码写进 PUBLIC_URL 了，别重复接
        self.assertEqual(with_secret("http://h:9797/abc", "abc"), "http://h:9797/abc")
        self.assertEqual(with_secret("http://h:9797", ""), "http://h:9797")
        self.assertEqual(with_secret("", "abc"), "")


class _ServerCase(unittest.TestCase):
    secret: str | None = SECRET

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "output").mkdir()
        (root / "output" / "latest.json").write_text(json.dumps(RESULT), encoding="utf-8")

        if self.secret is None:
            os.environ.pop("HDIPTV_SECRET", None)
        else:
            os.environ["HDIPTV_SECRET"] = self.secret
        self.addCleanup(os.environ.pop, "HDIPTV_SECRET", None)

        self.settings = load_settings(root)
        self.state = State(self.settings)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.tmp.cleanup)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path: str, *, html: bool = False, follow: bool = True):
        req = urllib.request.Request(self.url(path))
        req.add_header("Accept", "text/html" if html else "application/json")
        opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), exc.headers

    def post(self, path: str, payload: dict | None = None):
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


class TestWithSecret(_ServerCase):
    def test_root_denied(self):
        code, body, _ = self.get("/", html=True)
        self.assertEqual(code, 401)
        self.assertIn("身份认证失败", body)
        # 顺便确认没有把密码写进错误页
        self.assertNotIn(SECRET, body)

    def test_panel_served_under_secret(self):
        code, body, _ = self.get(f"/{SECRET}/", html=True)
        self.assertEqual(code, 200)
        self.assertIn("hd-iptv", body)
        # 资源必须是相对路径，否则会掉到根目录被拦
        self.assertIn('href="static/style.css"', body)

    def test_bare_secret_redirects_to_slash(self):
        code, _, headers = self.get(f"/{SECRET}", follow=False)
        self.assertIn(code, (301, 302))
        self.assertEqual(headers.get("Location"), f"/{SECRET}/")

    def test_wrong_secret_denied(self):
        code, _, _ = self.get("/wrong/playlist.m3u")
        self.assertEqual(code, 401)
        # 前缀对了但后面不对，也不能放过
        code, _, _ = self.get(f"/{SECRET}x/playlist.m3u")
        self.assertEqual(code, 401)

    def test_static_assets_protected(self):
        self.assertEqual(self.get("/static/app.js")[0], 401)
        code, body, _ = self.get(f"/{SECRET}/static/app.js")
        self.assertEqual(code, 200)
        self.assertIn("absUrl", body)

    def test_playlist_urls_carry_secret(self):
        code, body, _ = self.get(f"/{SECRET}/playlist.m3u")
        self.assertEqual(code, 200)
        self.assertIn(f"/{SECRET}/live/", body)
        self.assertIn(f"/{SECRET}/logo/", body)
        for line in body.splitlines():
            if line.startswith("http"):
                self.assertIn(f"/{SECRET}/", line)

    def test_api_protected(self):
        self.assertEqual(self.get("/api/status")[0], 401)
        code, body, _ = self.get(f"/{SECRET}/api/status")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["base"], self.url(f"/{SECRET}"))
        self.assertEqual(data["settings"]["secret"], SECRET)
        self.assertTrue(data["endpoints"]["m3u"].startswith(f"/{SECRET}/"))

    def test_post_protected(self):
        code, _ = self.post("/api/update")
        self.assertEqual(code, 401)

    def test_health_stays_open(self):
        # 容器的 HEALTHCHECK 打的就是这个地址，带上密码段会把自己判成不健康
        code, body, _ = self.get("/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_m3u8_redirect_keeps_secret(self):
        code, _, headers = self.get(f"/{SECRET}/m3u8", follow=False)
        self.assertIn(code, (301, 302))
        self.assertEqual(headers.get("Location"), f"/{SECRET}/playlist.m3u")


class TestPanelSecret(_ServerCase):
    def test_panel_can_change_secret(self):
        code, body = self.post(f"/{SECRET}/api/settings", {"secret": "newPass1"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"]["secret"], "newPass1")
        # 旧地址当场失效，新地址立刻可用
        self.assertEqual(self.get(f"/{SECRET}/api/status")[0], 401)
        self.assertEqual(self.get("/newPass1/api/status")[0], 200)
        # 落盘到 panel.json（在 .gitignore 里，不会跟着仓库走）
        saved = json.loads((self.settings.panel_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["secret"], "newPass1")

    def test_bad_secret_rejected(self):
        code, body = self.post(f"/{SECRET}/api/settings", {"secret": "带空格 的"})
        self.assertEqual(code, 400)
        self.assertIn("密码", json.loads(body)["error"])
        # 被拒绝之后密码保持原样，不会变成半截
        self.assertEqual(self.get(f"/{SECRET}/api/status")[0], 200)

    def test_empty_secret_turns_it_off(self):
        code, body = self.post(f"/{SECRET}/api/settings", {"secret": ""})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"]["secret"], "")
        self.assertEqual(self.get("/api/status")[0], 200)

    def test_other_settings_still_work(self):
        code, body = self.post(f"/{SECRET}/api/settings", {"interval_hours": 6})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"]["interval_hours"], 6)
        # 没提交 secret 就不该动它
        self.assertEqual(json.loads(body)["settings"]["secret"], SECRET)


class TestWithoutSecret(_ServerCase):
    secret = None

    def test_open_by_default(self):
        self.assertEqual(self.get("/")[0], 200)
        code, body, _ = self.get("/playlist.m3u")
        self.assertEqual(code, 200)
        self.assertIn("/live/", body)
        self.assertNotIn(f"/{SECRET}/", body)

    def test_api_works(self):
        code, body, _ = self.get("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"]["secret"], "")


if __name__ == "__main__":
    unittest.main()
