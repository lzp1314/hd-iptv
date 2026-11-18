"""官方源逐条日志文案：区分「没测速」与「真测过」。

咪咕/央视频这类播放时才换票的源，采集阶段 url 为空、探测时不发请求，
speed=0 / elapsed≈0 表示「没测」而非「测出 0」。日志不能再打成 0.0Mbps，
否则看着像限流或故障（用户实际已验证能播）。
"""

from __future__ import annotations

import unittest

from iptvhd.models import Stream
from iptvhd.pipeline import _official_log_line


class OfficialLogLineTest(unittest.TestCase):
    def test_deferred_source_says_measured_at_playback(self):
        # 咪咕：只登记频道，url 为空，探测瞬间判通
        s = Stream(url="", raw_name="山东教育", slug="m:123456",
                   playlist_only=True, trusted=True, height=720, ok=True)
        line = _official_log_line(s, 0.0001, 109, 127)
        self.assertIn("山东教育", line)
        self.assertIn("通 720P（播放时测速）", line)
        self.assertNotIn("Mbps", line)
        self.assertNotIn("0.0s", line)

    def test_deferred_source_without_height(self):
        s = Stream(url="", raw_name="熊猫频道1", slug="m:1",
                   playlist_only=True, trusted=True, ok=True)
        line = _official_log_line(s, 0.0, 1, 10)
        self.assertIn("通 播放时测速", line)
        self.assertNotIn("Mbps", line)

    def test_real_measured_source_keeps_speed(self):
        # 本省运营商源：真下过分片，照旧显示用时与速率
        s = Stream(url="http://edge.example/a.m3u8", raw_name="CCTV-1", height=1080,
                   speed_mbps=12.34, ok=True)
        line = _official_log_line(s, 0.23, 1, 10)
        self.assertIn("0.2s 通 1080P 12.3Mbps", line)
        self.assertNotIn("播放时测速", line)

    def test_failed_line_keeps_reason(self):
        s = Stream(url="http://edge.example/b.m3u8", raw_name="湖北卫视", ok=False,
                   error="HTTP 403")
        line = _official_log_line(s, 1.5, 2, 10)
        self.assertIn("1.5s 弃 HTTP 403", line)

    def test_inherited_line(self):
        s = Stream(url="http://edge.example/c.m3u8", raw_name="湖南卫视", ok=True)
        line = _official_log_line(s, None, 3, 10)
        self.assertIn("— 同台备用", line)


if __name__ == "__main__":
    unittest.main()
