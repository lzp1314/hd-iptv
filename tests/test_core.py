"""核心解析逻辑单测：不联网。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iptvhd import hls
from iptvhd.collectors import parse_m3u, parse_txt, parse_list
from iptvhd.models import Channel, Stream
from iptvhd.normalize import ChannelMatcher, normalize, parse_channels
from iptvhd.settings import ensure_defaults, load_settings
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
        # 显示名优先：tvg-name 是给 EPG 对表用的粗粒名字，同名会套在好几个台上
        self.assertEqual(streams[0].raw_name, "CCTV-1 综合")
        self.assertEqual(streams[0].alt_name, "CCTV-1")
        self.assertEqual(streams[0].group, "央视")
        self.assertEqual(streams[1].url, "http://s/2.m3u8")

    def test_parse_m3u_duplicate_tvg_name(self):
        """两个台的 tvg-name 相同时，不能靠它区分，必须看显示名。"""
        text = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="CCTV5" group-title="央视频道",CCTV-5体育\n'
            "http://s/5.m3u8\n"
            '#EXTINF:-1 tvg-name="CCTV5" group-title="央视频道",CCTV-5+体育赛事\n'
            "http://s/5p.m3u8\n"
        )
        streams = parse_m3u(text)
        self.assertEqual([s.raw_name for s in streams], ["CCTV-5体育", "CCTV-5+体育赛事"])

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

    def test_plus_sign_is_meaningful(self):
        # CCTV-5 和 CCTV-5+ 是两个不同的台，不能因为「+」被当成标点就合并
        self.assertNotEqual(normalize("CCTV-5+"), normalize("CCTV-5"))
        channels = parse_channels("央视,#genre#\nCCTV-5\nCCTV-5+\n")
        matcher = ChannelMatcher(channels)
        self.assertEqual(matcher.match("CCTV-5").channel.name, "CCTV-5")
        self.assertEqual(matcher.match("CCTV5+").channel.name, "CCTV-5+")

    def test_cgtn_languages_stay_apart(self):
        # 没有独立词条时，CGTN 法语会被「包含」规则吞进 CGTN
        channels = parse_channels(
            "央视,#genre#\nCGTN\nCGTN 法语\nCGTN 俄语\nCGTN 纪录\n"
        )
        matcher = ChannelMatcher(channels)
        for name in ("CGTN", "CGTN 法语", "CGTN 俄语", "CGTN 纪录"):
            self.assertEqual(matcher.match(name).channel.name, name)

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


class DefaultsTest(unittest.TestCase):
    """空 config/ 目录（Docker 挂载成空的样子）必须能自动补齐。"""

    def test_fills_empty_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            created = ensure_defaults(root)
            self.assertEqual(sorted(created), ["channels.txt", "config.toml", "sources.toml"])
            # 补齐后能真正加载出频道，不是空模板
            settings = load_settings(root)
            self.assertTrue(settings.channels_file.exists())
            self.assertGreater(len(parse_channels(settings.channels_file.read_text(encoding="utf-8"))), 10)

    def test_keeps_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            mine = root / "config" / "channels.txt"
            mine.write_text("央视,#genre#\nCCTV-1\n", encoding="utf-8")
            created = ensure_defaults(root)
            self.assertNotIn("channels.txt", created)
            self.assertEqual(mine.read_text(encoding="utf-8"), "央视,#genre#\nCCTV-1\n")
            # 缺的那个仍然补上
            self.assertIn("sources.toml", created)

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_defaults(root)
            self.assertEqual(ensure_defaults(root), [])


class BitrateTest(unittest.TestCase):
    def _resp(self, size):
        from iptvhd.net import Response

        return Response(headers={"content-length": str(size)} if size else {})

    def test_bitrate_from_segment_size(self):
        from iptvhd.probe import _estimate_bitrate

        stream = Stream(url="x", segment_duration=5.0)
        _estimate_bitrate(stream, self._resp(3_000_000))   # 3MB / 5s
        self.assertEqual(stream.bandwidth, 4_800_000)

    def test_no_duration_no_guess(self):
        from iptvhd.probe import _estimate_bitrate

        stream = Stream(url="x")
        _estimate_bitrate(stream, self._resp(3_000_000))
        self.assertEqual(stream.bandwidth, 0)

    def test_missing_content_length_is_ignored(self):
        from iptvhd.probe import _estimate_bitrate

        stream = Stream(url="x", segment_duration=6.0)
        _estimate_bitrate(stream, self._resp(0))
        self.assertEqual(stream.bandwidth, 0)

    def test_existing_bandwidth_is_kept(self):
        from iptvhd.probe import _estimate_bitrate

        stream = Stream(url="x", segment_duration=5.0, bandwidth=6_000_000)
        _estimate_bitrate(stream, self._resp(1_000_000))
        self.assertEqual(stream.bandwidth, 6_000_000)

    def test_reads_segment_seconds_from_playlist(self):
        from iptvhd.ysp import _segment_seconds

        text = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5.000,\na.ts\n"
        self.assertEqual(_segment_seconds(text), 5.0)
        only_target = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
        self.assertEqual(_segment_seconds(only_target), 4.0)
        self.assertEqual(_segment_seconds("#EXTM3U"), 0.0)


if __name__ == "__main__":
    unittest.main()
