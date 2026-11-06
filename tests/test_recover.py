"""回归：采集掉台时的补救、缺失统计、4K 频道命名。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from iptvhd import normalize, probe
from iptvhd.models import Channel, Stream
from iptvhd.pipeline import _carry_over, _missing
from iptvhd.settings import load_settings


def _settings_with_prev(tmp: Path, channels_payload: list, generated: str | None = None):
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "output").mkdir(parents=True, exist_ok=True)
    (tmp / "config" / "channels.txt").write_text("央视,#genre#\nCCTV-1\n湖南卫视\n", encoding="utf-8")
    payload = {
        "generated_at": generated or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "report": {"published": 2},
        "channels": channels_payload,
    }
    (tmp / "output" / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return load_settings(tmp)


class TestCarryOver(unittest.TestCase):
    def test_fills_channel_that_failed_this_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings_with_prev(
                Path(tmp),
                [
                    {
                        "name": "CCTV-1",
                        "group": "央视",
                        "streams": [
                            {
                                "url": "https://cdn.example.com/old.m3u8",
                                "slug": "cctv1",
                                "tvg_id": "CCTV1",
                                "height": 1080,
                                "width": 1920,
                                "bandwidth_kbps": 6000,
                                "codec": "h264",
                                "segment_duration": 5.0,
                            }
                        ],
                    }
                ],
            )
            channels = [Channel(name="CCTV-1", group="央视"), Channel(name="湖南卫视", group="卫视")]
            filled = _carry_over(settings, channels, lambda _m: None)

            self.assertEqual(filled, 1)
            self.assertEqual(len(channels[0].streams), 1)
            carried = channels[0].streams[0]
            self.assertTrue(carried.stale)
            self.assertTrue(carried.ok)
            self.assertEqual(carried.slug, "cctv1")
            self.assertEqual(carried.height, 1080)
            self.assertEqual(carried.bandwidth, 6_000_000)
            # 上一轮也没有的台不能凭空造出来
            self.assertEqual(channels[1].streams, [])

    def test_keeps_channel_that_already_has_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings_with_prev(
                Path(tmp),
                [{"name": "CCTV-1", "group": "央视", "streams": [{"url": "u", "slug": "cctv1"}]}],
            )
            fresh = Stream(url="https://new/x.m3u8", slug="cctv1", ok=True)
            channels = [Channel(name="CCTV-1", group="央视", streams=[fresh])]
            self.assertEqual(_carry_over(settings, channels, lambda _m: None), 0)
            self.assertEqual(channels[0].streams[0].url, "https://new/x.m3u8")

    def test_skips_when_previous_result_too_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now() - timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
            settings = _settings_with_prev(
                Path(tmp),
                [{"name": "CCTV-1", "group": "央视", "streams": [{"url": "u", "slug": "cctv1"}]}],
                generated=old,
            )
            channels = [Channel(name="CCTV-1", group="央视")]
            self.assertEqual(_carry_over(settings, channels, lambda _m: None), 0)
            self.assertEqual(channels[0].streams, [])

    def test_needs_slug_to_do_anything(self):
        """没有 slug 的流拼不出中转地址，继承过来也没用。"""
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings_with_prev(
                Path(tmp),
                [{"name": "CCTV-1", "group": "央视", "streams": [{"url": "https://x/a.m3u8"}]}],
            )
            channels = [Channel(name="CCTV-1", group="央视")]
            self.assertEqual(_carry_over(settings, channels, lambda _m: None), 0)


class TestMissing(unittest.TestCase):
    def test_explains_why_each_channel_is_absent(self):
        channels = [
            Channel(name="CCTV-1", group="央视", streams=[Stream(url="u", ok=True)]),
            Channel(name="湖南卫视", group="卫视"),
            Channel(name="兵团卫视", group="卫视"),
        ]
        missing = _missing(channels)
        self.assertEqual(len(missing), 2)
        by_name = {row["name"]: row["reason"] for row in missing}
        # 湖南卫视是央视频有的台，缺了那就是限流/故障
        self.assertIn("限流", by_name["湖南卫视"])
        # 兵团卫视上游根本没有，说清楚，别让人以为是程序坏了
        self.assertIn("没有这个台", by_name["兵团卫视"])

    def test_empty_when_all_present(self):
        channels = [Channel(name="CCTV-1", group="央视", streams=[Stream(url="u", ok=True)])]
        self.assertEqual(_missing(channels), [])


class TestUhdNaming(unittest.TestCase):
    def test_4k_channels_keep_their_identity(self):
        # 4K/8K 是频道名的一部分，不能被当成画质标记抹掉
        self.assertEqual(normalize.normalize("CCTV-4K 超高清"), "cctv4k")
        self.assertEqual(normalize.normalize("CCTV-8K 超高清"), "cctv8k")
        self.assertEqual(normalize.normalize("CCTV-16 4K"), "cctv164k")
        self.assertEqual(normalize.normalize("CCTV4K"), "cctv4k")

    def test_plain_channels_are_unaffected(self):
        self.assertEqual(normalize.normalize("CCTV-16"), "cctv16")
        self.assertEqual(normalize.normalize("CCTV-1 1080P 50FPS"), "cctv1")
        self.assertEqual(normalize.normalize("CCTV-5+"), "cctv5plus")

    def test_4k_and_plain_never_collide(self):
        matcher_channels = [
            Channel(name="CCTV-16", group="央视"),
            Channel(name="CCTV-16 4K", group="央视"),
            Channel(name="CCTV-4K 超高清", group="央视"),
        ]
        matcher = normalize.ChannelMatcher(matcher_channels)
        self.assertEqual(matcher.match("CCTV-16 4K").channel.name, "CCTV-16 4K")
        self.assertEqual(matcher.match("CCTV-16").channel.name, "CCTV-16")
        self.assertEqual(matcher.match("CCTV-4K").channel.name, "CCTV-4K 超高清")


class TestProbeSkipsUpstream(unittest.TestCase):
    """采集阶段验过的流，探测阶段不该再向上游要一次。"""

    def test_verified_recently_is_not_refetched(self):
        class Boom:
            def get(self, *a, **k):  # noqa: ANN002, ANN003
                raise AssertionError("不该再请求上游")

        import time

        stream = Stream(
            url="https://cdn/x.m3u8",
            playlist_only=True,
            first_segment="https://cdn/seg1.ts",
            verified_at=time.time(),
            hint_height=1080,
        )
        out = probe._probe_prefetched(stream, Boom(), 262144)
        self.assertTrue(out.ok)
        self.assertEqual(out.height, 1080)

    def test_stale_verification_refetches_manifest_not_old_segment(self):
        """超过窗口就重读清单取最新分片，而不是拿过期分片地址去撞。"""

        class FakeResp:
            def __init__(self, status=200, body=b""):
                self.status = status
                self.body = body
                self.url = "https://cdn/x.m3u8"
                self.headers = {"content-length": "200000"}
                self.ttfb_ms = 30
                self.elapsed_ms = 100
                self.error = ""

            @property
            def text(self):
                return self.body.decode()

            @property
            def speed_mbps(self):
                return 8.0

        manifest = b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nseg9.ts\n"

        class Fake:
            def __init__(self):
                self.calls = []

            def get(self, url, **k):  # noqa: ANN003
                self.calls.append(url)
                if url.endswith(".m3u8"):
                    return FakeResp(200, manifest)
                return FakeResp(200, b"\x00" * 20000)

        fake = Fake()
        stream = Stream(
            url="https://cdn/x.m3u8",
            playlist_only=True,
            first_segment="https://cdn/expired-seg1.ts",
            verified_at=1.0,  # 很久以前
            hint_height=1080,
        )
        out = probe._probe_prefetched(stream, fake, 262144)
        self.assertTrue(out.ok)
        self.assertIn("https://cdn/x.m3u8", fake.calls)
        self.assertTrue(any(c.endswith("seg9.ts") for c in fake.calls))
        # 关键：没有再去请求那个早已被回收的分片
        self.assertNotIn("https://cdn/expired-seg1.ts", fake.calls)


class TestDenyBreaker(unittest.TestCase):
    """出口被限流时要尽快停手，而不是把剩下几十个台挨个敲一遍。"""

    def setUp(self):
        from iptvhd import ysp

        self.ysp = ysp
        ysp.reset_deny()

    def tearDown(self):
        self.ysp.reset_deny()

    def test_trips_after_five_rejections(self):
        for _ in range(4):
            self.ysp.note_denied()
        self.assertFalse(self.ysp.denied())
        self.ysp.note_denied()
        self.assertTrue(self.ysp.denied())

    def test_success_resets_the_streak(self):
        for _ in range(4):
            self.ysp.note_denied()
        self.ysp.note_ok()
        self.ysp.note_denied()
        self.assertFalse(self.ysp.denied(), "成功一次就该重新计数")

    def test_play_info_stops_calling_upstream_when_denied(self):
        for _ in range(5):
            self.ysp.note_denied()
        with self.assertRaises(self.ysp.YspError) as ctx:
            self.ysp.play_info("2024078201", "600001859")
        self.assertIn("限流", str(ctx.exception))


class TestChannelUpgrade(unittest.TestCase):
    """容器里挂着的旧默认模板不会随镜像更新，得能自动换掉。"""

    OLD = """# 频道模板
# 这一份只保留「央视 + 卫视」。地方台、港澳台、体育赛事、平台直播都不在
# 范围里——频道越少，每轮探测越快，结果也越稳定。
央视,#genre#
CCTV-1,CCTV1
"""

    def test_replaces_untouched_default_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "channels.txt").write_text(self.OLD, encoding="utf-8")
            settings = load_settings(root)
            self.assertTrue(settings.channels_upgraded)
            text = (root / "config" / "channels.txt").read_text(encoding="utf-8")
            self.assertNotIn("频道越少，每轮探测越快", text)

    def test_leaves_user_edited_template_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            custom = "央视,#genre#\nCCTV-1\nCCTV-2\n兵团卫视\n"
            (root / "config" / "channels.txt").write_text(custom, encoding="utf-8")
            settings = load_settings(root)
            self.assertFalse(settings.channels_upgraded)
            self.assertEqual((root / "config" / "channels.txt").read_text(encoding="utf-8"), custom)


if __name__ == "__main__":
    unittest.main()
