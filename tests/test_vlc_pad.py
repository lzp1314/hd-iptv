"""VLC 垫片清单测试：结构、时长守恒、UA 判断、安全跳过。"""

from __future__ import annotations

import unittest

from iptvhd.relay import is_vlc_ua, pad_body, padded_playlist

PAD_URL = "http://nas.local:9797/secret/pad.ts"

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-TARGETDURATION:8
#EXTINF:7.600,
seg100.ts?token=abc
#EXTINF:4.240,
seg101.ts?token=abc
#EXTINF:5.000,
seg102.ts?token=abc
"""


class VlcPadTest(unittest.TestCase):
    def test_pad_body_is_ts_null_packets(self):
        body = pad_body()
        self.assertEqual(len(body) % 188, 0)
        self.assertGreater(len(body) // 188, 50)  # 明显多于 libVLC 一次解复用的读包数
        for i in range(0, len(body), 188):
            self.assertEqual(body[i], 0x47)          # TS 同步字节
            self.assertEqual(body[i + 1:i + 4], b"\x1f\xff\x10")  # PID 0x1FFF 空包

    def test_ua_detection(self):
        self.assertTrue(is_vlc_ua("Mozilla/5.0 VLC/3.0.20 LibVLC/3.0.20"))
        self.assertTrue(is_vlc_ua("Mozilla/5.0 (Linux) LibVLC/4.0.0 Ceau/1.0"))
        self.assertTrue(is_vlc_ua("vlc/3.0"))  # 大小写不敏感
        self.assertFalse(is_vlc_ua("Mozilla/5.0 Chrome/126.0 Safari/537.36"))
        self.assertFalse(is_vlc_ua(""))

    def test_playlist_ends_with_pad(self):
        out = padded_playlist(PLAYLIST, PAD_URL)
        self.assertIsNotNone(out)
        self.assertEqual(out.strip().splitlines()[-1], PAD_URL)

    def test_leading_pads(self):
        out = padded_playlist(PLAYLIST, PAD_URL)
        lines = out.splitlines()
        # 头四行是清单标签，紧接着两个前导垫片（EXTINF + URL 各一行）
        self.assertEqual(lines[4], "#EXTINF:0.010,")
        self.assertEqual(lines[5], PAD_URL)
        self.assertEqual(lines[6], "#EXTINF:0.010,")
        self.assertEqual(lines[7], PAD_URL)

    def test_duration_conserved_and_target_duration_capped(self):
        out = padded_playlist(PLAYLIST, PAD_URL)
        # 原片 7.600 → 声明 7.590，加垫片 0.010，合计仍是 7.600
        self.assertIn("#EXTINF:7.590,", out)
        self.assertIn("#EXT-X-TARGETDURATION:5", out)
        self.assertNotIn("#EXT-X-TARGETDURATION:8", out)

    def test_sequence_space_doubled(self):
        out = padded_playlist(PLAYLIST, PAD_URL)
        # 上游 MEDIA-SEQUENCE=100 → 100*2 - 2 = 198
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:198", out)

    def test_every_segment_followed_by_pad(self):
        out = padded_playlist(PLAYLIST, PAD_URL)
        lines = out.splitlines()
        pads = sum(1 for line in lines if line == PAD_URL)
        # 3 个整片各跟一个垫片，再加 2 个前导 = 5
        self.assertEqual(pads, 5)

    def test_unsafe_playlist_unchanged(self):
        for tag in ("KEY", "MAP", "BYTERANGE", "DISCONTINUITY", "ENDLIST", "PART", "STREAM-INF"):
            text = f"#EXTM3U\n#EXT-X-{tag}:x\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:5,\na.ts\n"
            self.assertIsNone(padded_playlist(text, PAD_URL), tag)

    def test_non_ts_segment_skipped(self):
        text = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:5,\na.m4s\n"
        self.assertIsNone(padded_playlist(text, PAD_URL))

    def test_missing_sequence_skipped(self):
        text = "#EXTM3U\n#EXTINF:5,\na.ts\n"
        self.assertIsNone(padded_playlist(text, PAD_URL))

    def test_empty_inputs(self):
        self.assertIsNone(padded_playlist("", PAD_URL))
        self.assertIsNone(padded_playlist(PLAYLIST, ""))

    def test_pad_url_carries_secret_prefix(self):
        # 垫片地址必须带密码段，否则播放器取垫片会被 _require 拦下
        out = padded_playlist(PLAYLIST, PAD_URL)
        self.assertIn("/secret/pad.ts", out)


if __name__ == "__main__":
    unittest.main()
