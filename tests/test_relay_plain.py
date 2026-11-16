"""普通上游源也走中转（``u:`` 前缀）相关的行为。

外省反馈的核心症状是「列表能刷出来，点进去源服务器返回错误」。根因不是源慢，
而是订阅里写的是**采集那一刻**的直连地址：聚合源认 UA、按 IP 限流、地址还带
时间戳，播放器自己拼出来的请求撞上校验就 403。列表里的
``#EXTVLCOPT:http-user-agent`` 只有 VLC 认，ExoPlayer 系直接当注释跳过。

所以这类源改成由本机取清单：这里守住三件事——前缀能区分、UA 用对、列表请求
能认出来。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from iptvhd import probe
from iptvhd.models import Stream, is_volatile
from iptvhd.relay import (
    RELAY_PREFIX,
    STATIC_PREFIX,
    plain_slug,
    slug_ua,
    static_slug,
    static_url,
)
from iptvhd.service import live_target
from iptvhd.settings import BROWSER_UA

PLAIN = "https://cdn.example.com/live/a.m3u8"
OFFICIAL = "http://ztecdn.hb.chinamobile.com:6060/a/index.m3u8"


class PrefixTest(unittest.TestCase):
    def test_both_prefixes_decode(self):
        self.assertEqual(static_url(plain_slug(PLAIN)), PLAIN)
        self.assertEqual(static_url(static_slug(OFFICIAL)), OFFICIAL)

    def test_slug_ua_follows_prefix(self):
        """``o:`` 用机顶盒 UA，``u:`` 用采集时那个浏览器 UA。

        两套 UA 不能混：运营商边缘只认机顶盒那种，聚合源认的是浏览器。
        """
        self.assertEqual(slug_ua(plain_slug(PLAIN)), BROWSER_UA)
        self.assertNotEqual(slug_ua(static_slug(OFFICIAL)), BROWSER_UA)

    def test_garbage_slug_returns_empty(self):
        self.assertEqual(static_url("c:5rmW5Y2X"), "")


class LiveTargetTest(unittest.TestCase):
    def test_plain_slug_is_recognized(self):
        """``u:`` 认不出来的话，播放器拿到的就是 404「未知频道」。"""
        slug = plain_slug(PLAIN)
        hit = live_target(slug)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], slug)

    def test_official_slug_is_recognized(self):
        slug = static_slug(OFFICIAL)
        hit = live_target(slug)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], slug)
        self.assertEqual(hit[1], "运营商源")


class VolatileSortTest(unittest.TestCase):
    """带签名、会过期的地址让位。

    这类地址探测那一刻必然通（刚签的），淘汰不掉；但写进订阅之后不归我们管，
    只能靠排序把长期地址顶上来。
    """

    def test_marker_detection(self):
        self.assertTrue(is_volatile("http://x/live.m3u8?time=1730993405&sign=ab"))
        self.assertTrue(is_volatile("http://x/live.m3u8?token=abc"))
        self.assertFalse(is_volatile("http://x/live.m3u8"))
        self.assertFalse(is_volatile(""))

    def test_volatile_sorts_after_stable_at_same_quality(self):
        stable = Stream(url="https://cdn.example.com/live/a.m3u8", height=1080,
                        speed_mbps=6.0, ok=True)
        volatile = Stream(url="https://cdn.example.com/live/b.m3u8?time=1&sign=x",
                          height=1080, speed_mbps=6.0, ok=True)
        self.assertLess(stable.sort_key(), volatile.sort_key())

    def test_official_still_wins_over_stability(self):
        """官方源即使带签名，也排在聚合源前面——它不换票、不夹广告。"""
        official = Stream(url="http://ztecdn.hb.chinamobile.com/a.m3u8?time=1",
                          height=1080, speed_mbps=6.0, trusted=True, ok=True)
        plain = Stream(url="https://cdn.example.com/a.m3u8", height=1080,
                       speed_mbps=6.0, ok=True)
        self.assertLess(official.sort_key(), plain.sort_key())


SHORT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:5.0,
a1.ts
#EXTINF:5.0,
a2.ts
#EXTINF:5.0,
a3.ts
"""


class ShortWindowRollTest(unittest.TestCase):
    """3 片窗口的列表要登记「待复核」。

    以前这里对 ``playlist_only`` 的源整批跳过，而订阅类源恰恰是短窗口的重灾区：
    列表不滚，播放器播完手里这三片就停在原地——用户说的「看一会儿就卡住」。
    """

    def _run(self, playlist_only: bool) -> Stream:
        stream = Stream(url="http://x/a.m3u8", raw_name="测试台",
                        playlist_only=playlist_only)
        resp = SimpleNamespace(url="http://x/a.m3u8", status=200, ttfb_ms=10,
                               speed_mbps=5.0)
        seg = SimpleNamespace(url="http://x/a1.ts", status=200, ttfb_ms=10,
                              speed_mbps=5.0, body=b"\x47" * 200_000, text="",
                              headers={})
        with mock.patch("iptvhd.probe._fetch", return_value=seg):
            probe._probe_hls(stream, client=None, body_text=SHORT, resp=resp,
                             sample_bytes=1024, deadline=None)
        return stream

    def test_playlist_only_source_is_checked(self):
        stream = self._run(playlist_only=True)
        self.assertTrue(stream.ok)
        self.assertEqual(stream.roll_url, "http://x/a.m3u8")
        self.assertEqual(stream.roll_seq, 100)

    def test_ordinary_source_is_checked_too(self):
        stream = self._run(playlist_only=False)
        self.assertEqual(stream.roll_url, "http://x/a.m3u8")


if __name__ == "__main__":
    unittest.main()
