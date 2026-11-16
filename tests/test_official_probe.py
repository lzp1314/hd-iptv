"""官方源串行慢探的三道省时闸。

背景：一个台常有两三条地址（内置表 + 本省自动源各给一条），143 条串行走完是
半小时，面板上看着就是「卡住不动」。这三道闸分别管「少探几条」「探不通就别
硬等」「日志要能定位到底是哪个台」。
"""

from pathlib import Path
import tempfile
from unittest import TestCase, mock

from iptvhd import probe
from iptvhd.models import Stream
from iptvhd.probe import probe_gentle
from iptvhd.settings import load_settings, Settings


def _streams(pairs):
    return [Stream(url=url, channel=ch) for ch, url in pairs]


def _probe_ok(stream, *_args, **_kwargs):
    stream.ok = True
    stream.status = 200
    stream.height = 1080
    stream.width = 1920
    stream.speed_mbps = 8.0
    stream.delay_ms = 20
    return stream


class SameChannelOnceTest(TestCase):
    """同一个台的多条地址只探前两条（默认），探通一条就收手。"""

    def test_probes_only_first_line_of_a_channel(self):
        streams = _streams([
            ("CCTV-1", "http://a/1.m3u8"),
            ("CCTV-1", "http://b/1.m3u8"),
            ("CCTV-1", "http://c/1.m3u8"),
            ("CCTV-2", "http://a/2.m3u8"),
            ("CCTV-2", "http://b/2.m3u8"),
        ])
        calls = []

        def fake(stream, *_args, **_kwargs):
            calls.append(stream.url)
            return _probe_ok(stream)

        with mock.patch("iptvhd.probe.probe_one", fake):
            self.assertEqual(probe_gentle(streams, client=None, gap=0), "")

        # 两个台各探一条，剩下的当备用线路
        self.assertEqual(calls, ["http://a/1.m3u8", "http://a/2.m3u8"])

    def test_backup_line_inherits_verdict(self):
        """备用线路不探，但要继承已探通那条的结论，否则就被 _filter 当成死链丢掉。"""
        streams = _streams([("CCTV-1", "http://a/1.m3u8"), ("CCTV-1", "http://b/1.m3u8")])

        with mock.patch("iptvhd.probe.probe_one", _probe_ok):
            probe_gentle(streams, client=None, gap=0)

        backup = streams[1]
        self.assertTrue(backup.ok)
        self.assertEqual(backup.height, 1080)
        self.assertEqual(backup.speed_mbps, 8.0)
        self.assertIn("未单独探测", backup.note)

    def test_falls_through_to_second_line_when_first_fails(self):
        calls = []

        def fake(stream, *_args, **_kwargs):
            calls.append(stream.url)
            if stream.url.endswith("/1.m3u8") and "b" in stream.url:
                return _probe_ok(stream)
            stream.ok = False
            stream.error = "HTTP 403"
            return stream

        streams = _streams([("CCTV-1", "http://a/1.m3u8"), ("CCTV-1", "http://b/1.m3u8")])
        with mock.patch("iptvhd.probe.probe_one", fake):
            probe_gentle(streams, client=None, gap=0)

        self.assertEqual(len(calls), 2)
        self.assertTrue(streams[1].ok)

    def test_tries_per_channel_is_respected(self):
        streams = _streams([("CCTV-1", f"http://a/{i}.m3u8") for i in range(5)])
        calls = []

        def fake(stream, *_args, **_kwargs):
            calls.append(stream.url)
            stream.ok = False
            stream.error = "HTTP 403"
            return stream

        with mock.patch("iptvhd.probe.probe_one", fake):
            probe_gentle(streams, client=None, gap=0, tries_per_channel=2)

        self.assertEqual(len(calls), 2)  # 一条网全死也不在同一台上耗五次
        self.assertTrue(all("同台已探线路均不可用" in s.error for s in streams[2:]))


class TimeoutCircuitTest(TestCase):
    """连续超时熔断：只认超时，不认 403。"""

    def test_aborts_after_consecutive_timeouts(self):
        streams = _streams([(f"CH{i}", f"http://x/{i}.m3u8") for i in range(20)])
        calls = []

        def fake(stream, *_args, **_kwargs):
            calls.append(stream.url)
            stream.ok = False
            stream.error = "探测超时"
            return stream

        with mock.patch("iptvhd.probe.probe_one", fake):
            reason = probe_gentle(streams, client=None, gap=0, timeout_streak=5)

        self.assertIn("连续 5 条超时", reason)
        self.assertLessEqual(len(calls), 10)  # 熔断后不再一条条等满时限
        self.assertTrue(all(not s.ok for s in streams))

    def test_403_does_not_trip_the_breaker(self):
        """403 是「这个台没了」，不是「这条网不通」，不能拿来熔断。"""
        streams = _streams([(f"CH{i}", f"http://x/{i}.m3u8") for i in range(20)])

        def fake(stream, *_args, **_kwargs):
            stream.ok = False
            stream.error = "HTTP 403"
            return stream

        with mock.patch("iptvhd.probe.probe_one", fake):
            reason = probe_gentle(streams, client=None, gap=0, timeout_streak=5)

        self.assertNotIn("连续", reason)

    def test_streak_resets_on_success(self):
        streams = _streams([(f"CH{i}", f"http://x/{i}.m3u8") for i in range(12)])
        seen = {"n": 0}

        def fake(stream, *_args, **_kwargs):
            seen["n"] += 1
            if seen["n"] % 4 == 0:
                return _probe_ok(stream)
            stream.ok = False
            stream.error = "连接超时"
            return stream

        with mock.patch("iptvhd.probe.probe_one", fake):
            # abort_ratio=1 把「失败率过半」那道闸关掉，只留连续超时这一道
            reason = probe_gentle(streams, client=None, gap=0, timeout_streak=5, abort_ratio=1.0)

        self.assertEqual(reason, "")  # 每隔四条成功一次，永远攒不到连续五条
        self.assertEqual(seen["n"], 12)


class LineLogTest(TestCase):
    """逐条日志：面板上「卡住」时唯一能定位问题台的信息。"""

    def test_reports_every_entry_including_inherited(self):
        streams = _streams([
            ("CCTV-1", "http://a/1.m3u8"),
            ("CCTV-1", "http://b/1.m3u8"),
            ("CCTV-2", "http://a/2.m3u8"),
        ])
        lines = []

        with mock.patch("iptvhd.probe.probe_one", _probe_ok):
            probe_gentle(
                streams, client=None, gap=0,
                on_line=lambda s, elapsed, done, total: lines.append(
                    (s.channel, elapsed, done, total)
                ),
            )

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1][2], 3)          # done 走到总数
        self.assertEqual(lines[-1][3], 3)
        self.assertIsNotNone(lines[0][1])          # 真探过的有用时
        self.assertIsNone(lines[1][1])             # 继承的没有用时


class TimeoutDetectionTest(TestCase):
    def test_timeout_hints(self):
        for text in ("探测超时", "读取超时（慢源）", "DNS 解析超时", "连接超时", "TimeoutError: x"):
            s = Stream(url="http://x/1.m3u8")
            s.error = text
            self.assertTrue(probe._looks_timeout(s), text)

    def test_non_timeout_errors(self):
        for text in ("HTTP 403", "HTTP 404", "响应体为空", "域名解析失败"):
            s = Stream(url="http://x/1.m3u8")
            s.error = text
            self.assertFalse(probe._looks_timeout(s), text)


class SettingsTest(TestCase):
    def _settings(self, text: str) -> Settings:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "config.toml").write_text(text, encoding="utf-8")
        return load_settings(tmp)

    def test_defaults(self):
        s = self._settings("[probe]\n")
        self.assertEqual(s.official_timeout, 5.0)
        self.assertEqual(s.official_tries_per_channel, 2)
        self.assertEqual(s.official_timeout_streak, 5)

    def test_clamped(self):
        s = self._settings(
            "[probe]\nofficial_timeout = 999\nofficial_tries_per_channel = 0\n"
            "official_timeout_streak = 1\n"
        )
        self.assertEqual(s.official_timeout, 30.0)
        self.assertEqual(s.official_tries_per_channel, 1)
        self.assertEqual(s.official_timeout_streak, 2)

    def test_env_overrides(self):
        s = self._settings("[probe]\n")
        with mock.patch.dict("os.environ", {"HDIPTV_OFFICIAL_TIMEOUT": "3"}):
            self.assertEqual(s.official_timeout, 3.0)
