"""核心解析逻辑单测：不联网。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iptvhd import hls
from iptvhd.collectors import parse_m3u, parse_txt, parse_list
from iptvhd.models import Channel, Stream
from iptvhd.normalize import ChannelMatcher, normalize, parse_channels
from iptvhd.writers import write_m3u, write_txt

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=3840x2160,CODECS="hvc1.2.4.L153,mp4a.40.2",FRAME-RATE=50
4k/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",FRAME-RATE=25
1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXTINF:6.006,
seg1.ts
#EXTINF:6.006,
seg2.ts
#EXT-X-ENDLIST
"""


class TestHls(unittest.TestCase):
    def test_parse_variants(self):
        variants = hls.parse_variants(MASTER, "http://h/x/master.m3u8")
        self.assertEqual(len(variants), 3)
        self.assertEqual(variants[0].height, 2160)
        self.assertIn("hvc1", variants[0].codecs)  # 逗号在引号内不能被切断
        self.assertEqual(variants[1].uri, "http://h/x/1080/index.m3u8")

    def test_best_variant(self):
        best = hls.best_variant(hls.parse_variants(MASTER, "http://h/x/"))
        self.assertIsNotNone(best)
        self.assertEqual(best.width, 3840)
        self.assertEqual(best.bandwidth, 8000000)

    def test_parse_media(self):
        info = hls.parse_media(MEDIA, "http://h/x/1080/")
        self.assertEqual(info["segments"], 2)
        self.assertEqual(info["first_segment"], "http://h/x/1080/seg1.ts")
        self.assertFalse(info["encrypted"])

    def test_guess_height(self):
        self.assertEqual(hls.guess_height("http://a/CCTV1-4K.m3u8"), 2160)
        self.assertEqual(hls.guess_height("http://a/x.m3u8", "湖南卫视1080P"), 1080)
        self.assertEqual(hls.guess_height("http://a/x.m3u8"), 0)


class TestCollectors(unittest.TestCase):
    def test_parse_m3u(self):
        text = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="cctv1" tvg-name="CCTV-1" tvg-logo="http://l/1.png" '
            'group-title="央视",CCTV-1 综合\n'
            "http://s/1.m3u8\n"
            "#EXTINF:-1 group-title=\"卫视\",湖南卫视HD\n"
            "http://s/2.m3u8\n"
        )
        streams = parse_m3u(text)
        self.assertEqual(len(streams), 2)
        self.assertEqual(streams[0].tvg_id, "cctv1")
        self.assertEqual(streams[0].raw_name, "CCTV-1")
        self.assertEqual(streams[0].group, "央视")
        self.assertEqual(streams[1].url, "http://s/2.m3u8")

    def test_parse_txt(self):
        text = "央视,#genre#\nCCTV-1,http://s/1.m3u8\n\n卫视,#genre#\n湖南卫视,http://s/2.m3u8\n"
        streams = parse_txt(text)
        self.assertEqual([s.raw_name for s in streams], ["CCTV-1", "湖南卫视"])
        self.assertEqual(streams[1].group, "卫视")

    def test_parse_list_dispatch(self):
        self.assertEqual(len(parse_list("#EXTM3U\n#EXTINF:-1,a\nhttp://s/1\n")), 1)
        self.assertEqual(len(parse_list("a,http://s/1\n")), 1)


class TestNormalize(unittest.TestCase):
    def test_quality_tags_removed(self):
        self.assertEqual(normalize("湖南卫视_HD"), "湖南卫视")
        self.assertEqual(normalize("湖南卫视 FHD 1080P"), "湖南卫视")
        self.assertEqual(normalize("CCTV-1 高清 50FPS"), "cctv1")

    def test_traditional(self):
        self.assertEqual(normalize("湖南衛視"), normalize("湖南卫视"))
        self.assertEqual(normalize("東方衛視"), normalize("东方卫视"))

    def test_no_cctv1_cctv11_confusion(self):
        channels = parse_channels("央视,#genre#\nCCTV-1\nCCTV-11\n")
        matcher = ChannelMatcher(channels)
        self.assertEqual(matcher.match("CCTV-11 戏曲").channel.name, "CCTV-11")
        self.assertEqual(matcher.match("CCTV-1 综合").channel.name, "CCTV-1")

    def test_alias_and_builtin(self):
        channels = parse_channels("央视,#genre#\nCCTV-1,央视一套\n")
        matcher = ChannelMatcher(channels)
        self.assertEqual(matcher.match("央视一套高清").channel.name, "CCTV-1")
        self.assertEqual(matcher.match("中央一台").channel.name, "CCTV-1")
        self.assertIsNone(matcher.match("不存在的台").channel)


class TestWriters(unittest.TestCase):
    def _channels(self):
        s1 = Stream(url="http://a/1080.m3u8", height=1080, width=1920, speed_mbps=6.0, delay_ms=30)
        s2 = Stream(url="http://a/4k.m3u8", height=2160, width=3840, speed_mbps=9.0, delay_ms=40)
        ch = Channel(name="CCTV-1", group="央视", streams=[s1, s2])
        ch.accept(4)
        return [ch, Channel(name="CCTV-2", group="央视")]

    def test_m3u(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.m3u"
            count = write_m3u(self._channels(), path, epg_url="http://e/epg.xml")
            self.assertEqual(count, 2)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith('#EXTM3U x-tvg-url="http://e/epg.xml"'))
            self.assertIn('tvg-name="CCTV-1"', text)
            self.assertIn("group-title=\"央视\"", text)
            # 4K 应排在 1080P 前面，且名字带画质后缀
            self.assertLess(text.index("4k.m3u8"), text.index("1080.m3u8"))
            self.assertIn("CCTV-1·4K", text)

    def test_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.txt"
            count = write_txt(self._channels(), path)
            self.assertEqual(count, 2)
            text = path.read_text(encoding="utf-8")
            self.assertIn("央视,#genre#", text)
            self.assertIn("CCTV-1,http://a/4k.m3u8", text)


if __name__ == "__main__":
    unittest.main()
