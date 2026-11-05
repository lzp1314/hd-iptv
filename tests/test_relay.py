"""中转层测试：改写、放行名单、失败处理。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iptvhd.relay import Relay, RelayError, allowed, decode, encode
from iptvhd.models import Channel, Stream
from iptvhd.writers import write_m3u

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:4.000,
seg-1.ts
#EXTINF:4.000,
https://hlsliveali-cdn.ysp.cctv.cn/seg-2.ts
"""


class FakeRelay(Relay):
    """不联网的 Relay：把上游响应写死。"""

    def __init__(self, pages: dict[str, tuple[int, str]]):
        super().__init__(lambda slug: ("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/1.m3u8", "qqlive"))
        self.pages = pages
        self.asked: list[str] = []

    def _fetch(self, url, ua):  # noqa: ANN001 - 测试替身
        self.asked.append(url)
        status, text = self.pages.get(url.rsplit("/", 1)[-1], (404, ""))
        return status, {"Content-Type": "application/vnd.apple.mpegurl"}, text.encode()


class CountingRelay(Relay):
    """解析次数可数的替身，用来验证「不按时间主动重解析」。"""

    def __init__(self):
        self.calls = 0
        super().__init__(self._resolver)

    def _resolver(self, slug):
        self.calls += 1
        return "https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/index.m3u8", "qqlive"

    def _fetch(self, url, ua):  # noqa: ANN001
        return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, MASTER.encode()


class FlakyResolver(Relay):
    """解析必失败，验证冷却期内不会反复敲门。"""

    def __init__(self):
        self.calls = 0
        super().__init__(self._resolver)

    def _resolver(self, slug):
        self.calls += 1
        raise RuntimeError("iretcode=14")


class RescuingRelay(Relay):
    """第一次分片被拒，第二次放行，验证重试。"""

    def __init__(self):
        super().__init__(lambda slug: ("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/1.m3u8", "qqlive"))
        self.attempts = 0

    def _fetch(self, url, ua):  # noqa: ANN001
        self.attempts += 1
        if self.attempts == 1:
            return 403, {}, b""
        return 200, {"Content-Type": "video/mp2t"}, b"TS-DATA"


class RelayTest(unittest.TestCase):
    def test_allowlist(self):
        self.assertTrue(allowed("https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8"))
        self.assertTrue(allowed("https://live-dtocnc-cdn.ysp.cctv.cn/A/1.m3u8"))
        self.assertTrue(allowed("https://ali-xwl.cztv.com/live/x.m3u8"))
        self.assertFalse(allowed("https://example.com/a.ts"))
        self.assertFalse(allowed("https://cctv.cn.example.com/a.ts"))
        self.assertFalse(allowed("ftp://hlsliveali-cdn.ysp.cctv.cn/a.ts"))
        self.assertFalse(allowed(""))

    def test_encode_roundtrip(self):
        url = "https://hlsliveali-cdn.ysp.cctv.cn/A/seg-1.ts?a=1&b=2"
        self.assertEqual(decode(encode(url)), url)
        self.assertNotIn("=", encode(url))  # 放进路径里不能带填充符
        self.assertEqual(decode("!!!not-base64!!!"), "")

    def test_playlist_rewrites_everything(self):
        relay = FakeRelay({"1.m3u8": (200, MASTER), "index.m3u8": (200, MEDIA)})
        text = relay.playlist("cctv1")
        self.assertIn("/relay/cctv1/p/", text)
        self.assertNotIn("ysp.cctv.cn", text)  # 播放器不该看到上游域名
        self.assertNotIn("\nindex.m3u8", text)

        # 再取子清单：分片和密钥也要一起改写
        token = text.split("/relay/cctv1/p/")[1].split("\n")[0].strip()
        self.assertTrue(decode(token).startswith("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/"))
        ctype, body = relay.resource("cctv1", token)
        media = body.decode()
        self.assertIn("mpegurl", ctype)
        self.assertNotIn("ysp.cctv.cn", media)
        self.assertIn('URI="/relay/cctv1/p/', media)
        self.assertEqual(media.count("/relay/cctv1/p/"), 3)  # 密钥 + 2 个分片

    def test_playlist_retries_once_on_403(self):
        relay = FakeRelay({"1.m3u8": (403, "")})
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertEqual(len(relay.asked), 2)  # 首次 + 换地址后重试

    def test_playlist_rejects_non_m3u8(self):
        relay = FakeRelay({"1.m3u8": (200, "<html>403</html>")})
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")

    def test_resource_blocks_foreign_host(self):
        relay = FakeRelay({})
        with self.assertRaises(RelayError):
            relay.resource("cctv1", encode("https://example.com/a.ts"))

    def test_resource_rejects_bad_token(self):
        relay = FakeRelay({})
        with self.assertRaises(RelayError):
            relay.resource("cctv1", "%%%")


class WriterRelayTest(unittest.TestCase):
    def test_relay_base_switches_to_local_url(self):
        stream = Stream(url="https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8", slug="cctv1",
                        user_agent="qqlive", height=1080)
        ch = Channel(name="CCTV-1", group="央视", streams=[stream])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.m3u"
            write_m3u([ch], path, relay_base="http://192.168.1.10:9797/")
            text = path.read_text(encoding="utf-8")
        self.assertIn("http://192.168.1.10:9797/live/cctv1.m3u8", text)
        self.assertNotIn("EXTVLCOPT", text)  # 走中转就不需要指定 UA 了
        self.assertNotIn("ysp.cctv.cn", text)

    def test_without_relay_base_keeps_direct_url(self):
        stream = Stream(url="https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8", slug="cctv1",
                        user_agent="qqlive", height=1080)
        ch = Channel(name="CCTV-1", group="央视", streams=[stream])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.m3u"
            write_m3u([ch], path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("ysp.cctv.cn", text)
        self.assertIn("EXTVLCOPT:http-user-agent=qqlive", text)


class RelayPolicyTest(unittest.TestCase):
    def test_address_is_reused_not_refreshed(self):
        """曾经每 3 分钟重解析一次，几次就把接口敲到限流，这里锁死行为。"""
        relay = CountingRelay()
        relay.playlist("cctv1")
        relay.playlist("cctv1")
        relay.playlist("cctv1")
        self.assertEqual(relay.calls, 1)

    def test_failed_resolve_enters_cooldown(self):
        relay = FlakyResolver()
        for _ in range(5):
            with self.assertRaises(RelayError):
                relay.upstream("cctv1")
        self.assertEqual(relay.calls, 1)  # 冷却期内只敲一次
        self.assertEqual(relay.failures, 1)

    def test_segment_retries_before_giving_up(self):
        relay = RescuingRelay()
        token = encode("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/seg-1.ts")
        ctype, body = relay.resource("cctv1", token)
        self.assertEqual(body, b"TS-DATA")
        self.assertIn("mp2t", ctype)
        self.assertEqual(relay.attempts, 2)

    def test_stats_shape(self):
        relay = CountingRelay()
        relay.playlist("cctv1")
        stats = relay.stats()
        self.assertEqual(stats["failures"], 0)
        self.assertIn("cctv1", stats["cached"])


if __name__ == "__main__":
    unittest.main()
