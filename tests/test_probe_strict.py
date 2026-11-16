"""探测加严：门槛系数、采样量、预算。

加严的目的是「宁可少一个台，也别给一条播不动的流」。这三项是配套的：
采样翻倍（测速更真）→ 门槛提高（挡掉刚好够呛的）→ 预算放宽（别让加严变成少台）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptvhd.models import Stream
from iptvhd.pipeline import _filter
from iptvhd.settings import Settings


def _settings(**probe) -> Settings:
    return Settings(root=Path(tempfile.mkdtemp()), data={"probe": probe})


def _stream(speed: float, height: int) -> Stream:
    s = Stream(url="http://a.example/x.m3u8", height=height)
    s.ok = True
    s.speed_mbps = speed
    return s


class TestSpeedFloor(unittest.TestCase):
    def test_1080p_needs_most_of_its_bandwidth(self):
        # 1080P 要 4 Mbps 才连续播，0.7 倍 = 2.8：2.5 的必须砍掉
        s = _settings()
        kept = _filter([_stream(2.5, 1080)], s)
        self.assertEqual(kept, [])

    def test_1080p_just_enough_passes(self):
        s = _settings()
        kept = _filter([_stream(3.0, 1080)], s)
        self.assertEqual(len(kept), 1)

    def test_720p_threshold(self):
        s = _settings()
        # 720P 需 2 Mbps，0.7 倍 = 1.4
        self.assertEqual(_filter([_stream(1.2, 720)], s), [])
        self.assertEqual(len(_filter([_stream(1.5, 720)], s)), 1)

    def test_min_speed_still_wins(self):
        # 低画质档的 0.7 倍会低于 min_speed，取大者，不能把底线放到 1 Mbps 以下
        s = _settings()
        self.assertEqual(_filter([_stream(0.7, 480)], s), [])

    def test_ratio_is_configurable(self):
        loose = _settings(speed_ratio=0.5)
        self.assertEqual(len(_filter([_stream(2.5, 1080)], loose)), 1)
        strict = _settings(speed_ratio=0.9)
        self.assertEqual(_filter([_stream(3.2, 1080)], strict), [])

    def test_ratio_clamped(self):
        # 写错成 0 会让所有源都过（max(0, ...) 等于没门槛），夹到 0.3
        self.assertAlmostEqual(_settings(speed_ratio=0).speed_ratio, 0.3)
        self.assertAlmostEqual(_settings(speed_ratio=5).speed_ratio, 1.0)
        self.assertAlmostEqual(_settings(speed_ratio="abc").speed_ratio, 0.7)

    def test_unknown_speed_not_filtered(self):
        # 没测到速率的流不能因为门槛提高就被砍：它可能是分片限流，不是跑不动
        s = _settings()
        self.assertEqual(len(_filter([_stream(0.0, 1080)], s)), 1)


class TestDefaults(unittest.TestCase):
    def test_sample_bytes_doubled(self):
        self.assertEqual(Settings(root=Path(tempfile.mkdtemp())).sample_bytes, 524288)

    def test_budget_has_headroom(self):
        # 采样翻倍后预算必须跟着动，否则长尾会把后面的台挤掉
        self.assertEqual(Settings(root=Path(tempfile.mkdtemp())).probe_budget, 210)

    def test_env_overrides(self):
        s = Settings(root=Path(tempfile.mkdtemp()))
        with patch.dict("os.environ", {"HDIPTV_SAMPLE_BYTES": "262144", "HDIPTV_SPEED_RATIO": "0.5"}):
            self.assertEqual(s.sample_bytes, 262144)
            self.assertAlmostEqual(s.speed_ratio, 0.5)


class TestShortWindow(unittest.TestCase):
    def test_window_widened(self):
        from iptvhd import probe

        # 7~8 片的短窗口源以前完全不复核，正是「播一会儿就停」那一类
        self.assertGreaterEqual(probe._SHORT_WINDOW, 8)


if __name__ == "__main__":
    unittest.main()
