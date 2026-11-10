"""运营商官方源（移动/电信 IPTV 边缘）相关逻辑的测试。

这一路的特殊之处只有三条，每条都配一个测试守住：

1. 302 之后只给 IPv6 边缘 —— direct 模式也必须本机中转，裸 IPv6 不能丢给播放器；
2. 每份清单都换一个新的会话参数 —— 窗口里同一片要跟着换成新地址；
3. 并发探测会被限成 302 死循环 —— 只能串行慢探，失败率过高要整体收手。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from iptvhd import pipeline
from iptvhd.collectors import parse_m3u
from iptvhd.models import Stream
from iptvhd.normalize import ChannelMatcher, load_channels
from iptvhd.probe import probe_gentle
from iptvhd.relay import Relay, _parse_media, _Window, allowed, static_slug, static_url

V6_BASE = "http://[2409:8087:4c00:22:6::9]:6610/170000001115/UmaiCHAN1/index.m3u8"

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:57524
#EXTINF:10.000,
seg-57524.ts
#EXTINF:10.000,
seg-57525.ts
"""

MEDIA_B = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:57524
#EXTINF:10.000,
seg-57524.ts?IASHttpSessionId=SESS-B
#EXTINF:10.000,
seg-57525.ts?IASHttpSessionId=SESS-B
"""


class _PageRelay(Relay):
    """不联网的中转：上游地址由测试指定，页面内容写死。"""

    def __init__(self, base: str, pages: dict[str, tuple[int, str]], **options):
        super().__init__(lambda slug: (base, "okhttp/3.12.0"), warm=False, **options)
        self.pages = pages
        self.asked: list[str] = []

    def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
        self.asked.append(url)
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]
        status, text = self.pages.get(name, (404, ""))
        return status, {"Content-Type": "application/vnd.apple.mpegurl"}, text.encode(), url


class Ipv6TargetTest(unittest.TestCase):
    """IPv6 边缘：direct 模式也要转手，IPv4 照旧直连。"""

    def test_ipv6_upstream_is_relayed_even_in_direct_mode(self):
        relay = _PageRelay(V6_BASE, {"index.m3u8": (200, MEDIA)})
        text = relay.playlist("cctv1")
        self.assertIn("/relay/cctv1/p/", text)
        self.assertNotIn("[2409:8087", text)  # 裸 IPv6 不能出现在给播放器的清单里
        self.assertEqual(relay.stats()["mode"], "direct")

    def test_ipv4_upstream_still_goes_direct(self):
        relay = _PageRelay("http://1.2.3.4:8080/live/index.m3u8",
                           {"index.m3u8": (200, MEDIA)})
        text = relay.playlist("cctv1")
        self.assertNotIn("/relay/", text)
        self.assertIn("http://1.2.3.4:8080/live/seg-57524.ts", text)


class AllowListTest(unittest.TestCase):
    def test_carrier_ipv6_is_allowed(self):
        self.assertTrue(allowed("http://[2409:8087:4c00:22:6::9]:6610/a/1.ts?x=1"))
        self.assertTrue(allowed("https://ztecdn.hb.chinamobile.com:6060/a/index.m3u8"))

    def test_foreign_ipv6_is_not_allowed(self):
        # 放行名单不能变成「任意地址都能借道」的中转口
        self.assertFalse(allowed("http://[2a00:1450:4001::1]:80/a.ts"))
        self.assertFalse(allowed("http://[::1]:80/a.ts"))


class WindowSessionTest(unittest.TestCase):
    """同一片换地址（新会话）时要跟着换，别留已经作废的那份。"""

    def test_same_segment_takes_newer_url(self):
        window = _Window()
        head, seq, segs = _parse_media(MEDIA, "http://[2409::1]:6610/x/")
        window.feed(head, seq, segs, time.time())

        head_b, seq_b, segs_b = _parse_media(MEDIA_B, "http://[2409::1]:6610/x/")
        window.feed(head_b, seq_b, segs_b, time.time())

        text = window.text()
        self.assertIn("SESS-B", text)
        self.assertNotIn("seg-57524.ts\n", text)
        # 序号和片数不能被这次替换搞乱
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:57524", text)
        self.assertEqual(text.count("#EXTINF"), 2)


class SharedEndpointTest(unittest.TestCase):
    """一址多台 = 广告农场 / 占位源，整批剔除。"""

    @staticmethod
    def _stream(channel: str, final: str) -> Stream:
        s = Stream(url="http://example.com/x.m3u8", channel=channel)
        s.final_url = final
        s.ok = True
        s.height = 1080
        return s

    def test_three_channels_on_one_endpoint_are_dropped(self):
        streams = [
            self._stream("CCTV-4", "http://47.97.252.137:81/applive/mkt.m3u8?app=a"),
            self._stream("CCTV-5", "http://47.97.252.137:81/applive/mkt.m3u8?app=b"),
            self._stream("CCTV-7", "http://47.97.252.137:81/applive/mkt.m3u8?app=c"),
            self._stream("CCTV-1", "http://cdn.example.com/live/cctv1.m3u8"),
        ]
        dropped = pipeline._drop_shared_endpoints(streams, lambda _: None)
        self.assertEqual(dropped, 3)
        survivors = [s.channel for s in streams if s.ok]
        self.assertEqual(survivors, ["CCTV-1"])

    def test_two_channels_are_left_alone(self):
        # 两个台偶尔撞一次可能是源作者写错，够不上「农场」的判断
        streams = [
            self._stream("CCTV-4", "http://cdn.example.com/live/x.m3u8"),
            self._stream("CCTV-5", "http://cdn.example.com/live/x.m3u8"),
        ]
        self.assertEqual(pipeline._drop_shared_endpoints(streams, lambda _: None), 0)
        self.assertTrue(all(s.ok for s in streams))

    def test_same_host_different_paths_are_fine(self):
        streams = [
            self._stream("CCTV-4", "http://cdn.example.com/live/cctv4.m3u8"),
            self._stream("CCTV-5", "http://cdn.example.com/live/cctv5.m3u8"),
            self._stream("CCTV-7", "http://cdn.example.com/live/cctv7.m3u8"),
        ]
        self.assertEqual(pipeline._drop_shared_endpoints(streams, lambda _: None), 0)


class RelayRoutingTest(unittest.TestCase):
    """官方源必须写成本机地址，否则没有 IPv6 的播放器直接放不出来。"""

    def test_trusted_stream_gets_static_slug(self):
        s = Stream(url="http://ztecdn.hb.chinamobile.com:6060/a/index.m3u8",
                   channel="CCTV-1", trusted=True)
        self.assertEqual(pipeline._route_official_through_relay([s]), 1)
        self.assertTrue(s.slug.startswith("o:"))
        self.assertEqual(static_url(s.slug), s.url)

    def test_ipv6_final_url_gets_slug_without_trust(self):
        s = Stream(url="http://plain.example.com/a.m3u8", channel="CCTV-1")
        s.final_url = "http://[2409:8087::9]:6610/a/index.m3u8"
        self.assertEqual(pipeline._route_official_through_relay([s]), 1)
        self.assertTrue(s.slug.startswith("o:"))

    def test_ordinary_source_is_left_direct(self):
        s = Stream(url="https://cdn.example.com/live/a.m3u8", channel="CCTV-1")
        s.final_url = "https://cdn.example.com/live/a.m3u8"
        self.assertEqual(pipeline._route_official_through_relay([s]), 0)
        self.assertEqual(s.slug, "")


class TrustedOrderTest(unittest.TestCase):
    def test_official_source_sorts_first(self):
        official = Stream(url="http://a/1.m3u8", channel="CCTV-1", trusted=True)
        official.ok = True
        official.height = 1080
        official.speed_mbps = 30.0
        mirror = Stream(url="http://b/1.m3u8", channel="CCTV-1")
        mirror.ok = True
        mirror.height = 1080
        mirror.speed_mbps = 30.0
        self.assertLess(official.sort_key(), mirror.sort_key())


class GentleProbeTest(unittest.TestCase):
    def test_aborts_when_failure_rate_is_high(self):
        streams = [Stream(url=f"http://x/{i}.m3u8", channel="CCTV-1") for i in range(20)]
        calls = []

        def fake_probe(stream, *_args, **_kwargs):
            calls.append(stream)
            stream.ok = False
            return stream

        with mock.patch("iptvhd.probe.probe_one", fake_probe):
            aborted = probe_gentle(streams, client=None, gap=0)

        self.assertTrue(aborted)
        self.assertEqual(len(calls), 8)  # min_tries 一到就收手，不是把 20 条全打完
        self.assertTrue(all("限流" in s.error for s in streams[8:]))

    def test_keeps_going_while_mostly_healthy(self):
        streams = [Stream(url=f"http://x/{i}.m3u8", channel="CCTV-1") for i in range(10)]

        def fake_probe(stream, *_args, **_kwargs):
            stream.ok = True
            return stream

        with mock.patch("iptvhd.probe.probe_one", fake_probe):
            self.assertFalse(probe_gentle(streams, client=None, gap=0))


class ShippedListTest(unittest.TestCase):
    """随镜像发布的那份运营商清单，必须真能对上频道模板。"""

    def test_default_list_matches_channel_template(self):
        root = Path(__file__).resolve().parent.parent
        streams = parse_m3u((root / "iptvhd/defaults/hubei-mobile.m3u").read_text(encoding="utf-8"))
        channels = load_channels(root / "config/channels.txt")
        matcher = ChannelMatcher(channels)

        hit: dict[str, int] = {}
        unmatched = []
        for s in streams:
            result = matcher.match(s.raw_name)
            if result.channel is None and s.alt_name:
                result = matcher.match(s.alt_name)
            if result.channel is None:
                unmatched.append(s.raw_name)
            else:
                hit[result.channel.name] = hit.get(result.channel.name, 0) + 1

        self.assertGreaterEqual(len(hit), 45)          # 央视 17 + 卫视主力
        self.assertLessEqual(len(unmatched), 3)        # 三沙/兵团/厦门不在模板里，允许
        self.assertTrue(all(n == 1 for n in hit.values()))  # 不许一个台占两条


class LiveRouteTest(unittest.TestCase):
    """``/live/<slug>.m3u8`` 的路由判定。

    0.9.13 的翻车点：官方源接进来之后，列表里写的是 ``/live/o:<base64>``，
    而路由只认央视频那张频道表，于是整批回 404，播放器只看得到「源服务器
    返回错误」。这个测试守住「两套 slug 都得认」。
    """

    def test_static_slug_is_accepted(self):
        from iptvhd.service import live_target

        slug = static_slug(V6_BASE)
        hit = live_target(slug)
        self.assertIsNotNone(hit)
        key, name = hit
        self.assertEqual(static_url(key), V6_BASE)
        self.assertEqual(name, "运营商源")

    def test_percent_encoded_slug_is_decoded(self):
        """播放器会把 o: 编码成 o%3A，还原之后才认得出来。"""
        from urllib.parse import quote

        from iptvhd.service import live_target

        hit = live_target(quote(static_slug(V6_BASE), safe=""))
        self.assertIsNotNone(hit)
        self.assertEqual(static_url(hit[0]), V6_BASE)

    def test_ysp_slug_still_works(self):
        from iptvhd.service import live_target

        hit = live_target("cctv1")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "cctv1")

    def test_unknown_slug_is_rejected(self):
        from iptvhd.service import live_target

        self.assertIsNone(live_target("这不是任何台"))
