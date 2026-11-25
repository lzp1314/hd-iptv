"""央视频限流治理：扫台本地挡、HEAD 不打上游、失败少敲几枪。

这些用例锁住的是「请求数」而不是播放结果：播放器预览/检测会把几十个台
挨个点一遍，真正把 NAS 出口打进 403 的是这些白打的请求。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from iptvhd import ysp
from iptvhd.net import Response
from iptvhd.relay import Relay, RelayError, ScanGuard, _TICKET_BUDGET
from iptvhd.service import State, make_handler
from iptvhd.settings import load_settings


class ScanGuardTest(unittest.TestCase):
    def test_six_new_channels_then_local_block(self):
        guard = ScanGuard(limit=6, window=10.0, cooldown=30.0)
        now = 1000.0
        for i in range(6):
            ok, _ = guard.check("192.168.1.2|Player", f"c{i}", now)
            self.assertTrue(ok, f"第 {i + 1} 个台应当放行")

        ok, retry = guard.check("192.168.1.2|Player", "c6", now + 0.1)
        self.assertFalse(ok, "第 7 个新台必须在本地被挡")
        self.assertGreater(retry, 0)

        # 已经在看的台继续续播，不算扫台
        ok, _ = guard.check("192.168.1.2|Player", "c0", now + 0.2)
        self.assertTrue(ok)

        # 别的客户端不受牵连
        ok, _ = guard.check("192.168.1.3|Player", "other", now + 0.2)
        self.assertTrue(ok)

        # 冷却过后恢复
        ok, _ = guard.check("192.168.1.2|Player", "c7", now + 31.0)
        self.assertTrue(ok)

    def test_old_touches_expire(self):
        guard = ScanGuard(limit=3, window=5.0, cooldown=2.0)
        for i in range(3):
            self.assertTrue(guard.check("ip|ua", f"c{i}", 100.0)[0])
        self.assertTrue(guard.check("ip|ua", "c3", 106.0)[0], "窗口和冷却都过了就不算连扫")


class BudgetTest(unittest.TestCase):
    def test_ticket_budget_allows_normal_playback(self):
        """预算只管批量扫台，不能卡住正在看的人。

        曾经降到 20，结果一个台被 403 打散后要连换几次票才接得上，20 张在
        中途就见底，换不到票只能干等——画面直接定住。挡住扫台的是 ScanGuard，
        不是这张总账。
        """
        self.assertEqual(_TICKET_BUDGET, 30, "30 张/分钟，给正常播放留足余量")

    def test_global_cooldown_still_allows_expired_watch(self):
        """出口冷却只挡「从没成功过」的台；在播台地址过期要换票照样放行。

        否则在播台地址一过 TTL 就被冻住，而它手里只有几秒缓冲——正是
        「播一分多钟后彻底不动、换回来也不播」的来源。
        """
        calls: list[str] = []

        def resolver(slug: str):
            calls.append(slug)
            return "https://cdn.example.com/live/index.m3u8", "qqlive"

        relay = Relay(resolver, warm=False, upstream_denied=lambda: True,
                      addr_ttl=0.0)
        # 从没成功过：冷却期一枪不打
        with self.assertRaises(RelayError):
            relay.upstream("cctv1")
        self.assertEqual(calls, [])

        # 有缓存但地址已过期（addr_ttl=0），在播台必须还能换票
        relay._cache["cctv1"] = (time.time() - 10, "https://cdn.example.com/old.m3u8", "qqlive")
        url, _ua = relay.upstream("cctv1")
        self.assertEqual(url, "https://cdn.example.com/live/index.m3u8")
        self.assertEqual(calls, ["cctv1"], "在播台换票不该被出口冷却挡死")

    def test_global_cooldown_blocks_cold_start_only(self):
        calls: list[str] = []

        def resolver(slug: str):
            calls.append(slug)
            return "https://cdn.example.com/live/index.m3u8", "qqlive"

        relay = Relay(resolver, warm=False, upstream_denied=lambda: True)
        with self.assertRaises(RelayError):
            relay.upstream("cctv1")
        self.assertEqual(calls, [], "上游冷却时新频道一枪不打")

        relay._cache["cctv1"] = (time.time(), "https://cdn.example.com/old.m3u8", "qqlive")
        url, _ua = relay.upstream("cctv1")
        self.assertEqual(url, "https://cdn.example.com/old.m3u8")
        self.assertEqual(calls, [], "有旧地址就续播，不重演 30 秒整台冻结的卡顿")


class YspRequestCountTest(unittest.TestCase):
    def test_default_attempts_recovered_and_manifest_has_deadline(self):
        """容错回到 3 轮。

        压到 2 轮时容错砍掉三分之一，CDN 节点一抖动就判死，接着换入口把票
        也换得更勤，反而更容易撞上限流。
        """
        client = mock.Mock()
        client.get.return_value = Response(url="u", status=403, body=b"")
        info = {"playurl": "https://cdn/t/c.m3u8"}
        with mock.patch.object(ysp, "play_info", return_value=info) as play, \
                mock.patch.object(ysp, "pace"), \
                mock.patch.object(ysp.time, "sleep"):
            with self.assertRaises(ysp.YspError):
                ysp.resolve("1", "2", "fhd", client)
        self.assertEqual(play.call_count, 3, "默认三轮取票")
        self.assertTrue(
            any("deadline" in call.kwargs for call in client.get.call_args_list),
            "取清单仍要有总时限，挂住的节点不能无限占着",
        )
        self.assertGreaterEqual(ysp.MANIFEST_TIMEOUT, 8.0, "时限不能压到几秒，慢节点会被判死")


class HttpGovernanceTest(unittest.TestCase):
    """起真服务确认 HEAD 与扫台闸门在 HTTP 层真的生效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "output").mkdir()
        (root / "output" / "result.json").write_text(
            json.dumps({"generated_at": "", "channels": []}), encoding="utf-8"
        )
        os.environ.pop("HDIPTV_SECRET", None)
        self.addCleanup(os.environ.pop, "HDIPTV_SECRET", None)
        self.state = State(load_settings(root))
        self.slugs = [row[0] for row in ysp.CHANNELS[:8]]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.addCleanup(self.httpd.server_close)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _get(self, slug: str, method: str = "GET"):
        req = urllib.request.Request(f"{self.base}/live/{slug}.m3u8", method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_head_does_not_touch_upstream(self):
        with mock.patch.object(self.state.relay, "playlist_info",
                               side_effect=AssertionError("HEAD 不该取上游清单")):
            code, body = self._get("cctv1", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")

    def test_scan_is_rejected_before_upstream(self):
        with mock.patch.object(self.state.relay, "playlist_info",
                               return_value=("#EXTM3U\n", False, 1)) as fetch:
            for slug in self.slugs[:6]:
                code, _ = self._get(slug)
                self.assertEqual(code, 200, f"{slug} 应正常放行")
            code, body = self._get(self.slugs[6])
            # 已经在看的台继续放行：冷却只拦新频道，不把观众一起挡在门外
            retry_code, _ = self._get(self.slugs[0])
        self.assertEqual(code, 429)
        self.assertIn("批量请求", body.decode("utf-8"))
        self.assertEqual(retry_code, 200)
        self.assertEqual(fetch.call_count, 7, "被挡的第 7 个台没打上游")


class WarmEndpointTest(unittest.TestCase):
    """播放器主动指路（P3）：/warm/<slug> 只排任务、立刻回 202。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "output").mkdir()
        (root / "output" / "result.json").write_text(
            json.dumps({"generated_at": "", "channels": []}), encoding="utf-8"
        )
        os.environ.pop("HDIPTV_SECRET", None)
        self.addCleanup(os.environ.pop, "HDIPTV_SECRET", None)
        self.state = State(load_settings(root))
        self.slugs = [row[0] for row in ysp.CHANNELS[:8]]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _warm(self, slug: str):
        req = urllib.request.Request(f"{self.base}/warm/{slug}")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_accepted_immediately(self):
        """必须回 202 且不阻塞：播放器在换台路径上等不起一次上游往返。"""
        calls = []
        with mock.patch.object(self.state.relay, "warm_async", side_effect=calls.append):
            code, body = self._warm("cctv2")
        self.assertEqual(code, 202)
        self.assertEqual(body, b"")
        self.assertEqual(calls, ["cctv2"])

    def test_unknown_channel(self):
        code, _ = self._warm("no-such-channel")
        self.assertEqual(code, 404)

    def test_accepts_m3u8_suffix(self):
        """播放器最省事的写法是「把 /live/ 换成 /warm/」，扩展名会一起过来。"""
        calls = []
        with mock.patch.object(self.state.relay, "warm_async", side_effect=calls.append):
            code, _ = self._warm("cctv2.m3u8")
        self.assertEqual(code, 202)
        self.assertEqual(calls, ["cctv2"])

    def test_warm_is_not_scan(self):
        """预热不算扫台：连发 8 个不该触发 429，也不影响随后的正常换台。"""
        with mock.patch.object(self.state.relay, "warm_async", side_effect=lambda slug: None):
            for slug in self.slugs:
                code, _ = self._warm(slug)
                self.assertEqual(code, 202, f"{slug} 的预热请求不该被挡")
        self.assertEqual(self.state.scan_guard.blocked, 0)

    def test_disabled_switch_still_returns_202(self):
        """关掉开关时静默接受：播放器不用为这个开关改逻辑。"""
        with mock.patch.object(
            type(self.state.settings), "warm_next", new_callable=mock.PropertyMock, return_value=False
        ), mock.patch.object(self.state.relay, "warm_async") as warm:
            code, _ = self._warm("cctv2")
        self.assertEqual(code, 202)
        warm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
