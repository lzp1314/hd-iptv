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
        first_pad = lines.index("#EXTINF:0.010,")
        # 第一个真实片之前只有两个前导垫片；直播起点标记排在垫片前面
        self.assertEqual(lines[first_pad + 1], PAD_URL)
        self.assertEqual(lines[first_pad + 2], "#EXTINF:0.010,")
        self.assertEqual(lines[first_pad + 3], PAD_URL)
        self.assertTrue(any(line.startswith("#EXT-X-START:") for line in lines[:first_pad]))
        self.assertIn("seg100.ts?token=abc", lines[first_pad + 5])

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

    def test_start_tag_preserved_and_floored(self):
        # 原清单给的起点要原样带过来
        text = PLAYLIST.replace(
            "#EXT-X-TARGETDURATION:8",
            "#EXT-X-TARGETDURATION:8\n#EXT-X-START:TIME-OFFSET=-18.0",
        )
        out = padded_playlist(text, PAD_URL)
        self.assertIn("#EXT-X-START:TIME-OFFSET=-18.0", out)

        # 起点太贴直播边缘时，按 3 个 TARGETDURATION 往后退
        text = PLAYLIST.replace(
            "#EXT-X-TARGETDURATION:8",
            "#EXT-X-TARGETDURATION:8\n#EXT-X-START:TIME-OFFSET=-6.0",
        )
        self.assertIn("#EXT-X-START:TIME-OFFSET=-15.0", padded_playlist(text, PAD_URL))

        # 原清单没给起点时按窗口总时长合成，且不超过窗口长度
        self.assertIn("#EXT-X-START:TIME-OFFSET=-15.0", padded_playlist(PLAYLIST, PAD_URL))
        short = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4,\na.ts\n"
        self.assertIn("#EXT-X-START:TIME-OFFSET=-4.0", padded_playlist(short, PAD_URL))

    def test_program_date_time_kept_on_real_segments(self):
        text = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:7
#EXTINF:5.000,
#EXT-X-PROGRAM-DATE-TIME:2026-11-24T07:00:00.000Z
a.ts
#EXTINF:5.000,
#EXT-X-PROGRAM-DATE-TIME:2026-11-24T07:00:05.000Z
b.ts
"""
        # 上游把 PDT 写在 EXTINF 与 URI 之间时也认
        out = padded_playlist(text, PAD_URL)
        self.assertIsNotNone(out)
        self.assertIn("#EXT-X-PROGRAM-DATE-TIME:2026-11-24T07:00:00.000Z", out)
        self.assertIn("#EXT-X-PROGRAM-DATE-TIME:2026-11-24T07:00:05.000Z", out)
        self.assertEqual(out.count("#EXT-X-PROGRAM-DATE-TIME:"), 2)  # 垫片不带时间戳

    def test_discontinuity_no_longer_skips_playlist(self):
        text = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:9
#EXTINF:5.000,
a.ts
#EXT-X-DISCONTINUITY
#EXTINF:5.000,
b.ts
"""
        out = padded_playlist(text, PAD_URL)
        self.assertIsNotNone(out)
        lines = out.splitlines()
        pos = lines.index("#EXT-X-DISCONTINUITY")
        self.assertEqual(lines[pos + 1], "#EXTINF:4.990,")
        self.assertEqual(lines[pos + 2], "b.ts")
        self.assertEqual(out.count("#EXT-X-DISCONTINUITY"), 1)

    def test_unsafe_playlist_unchanged(self):
        for tag in ("KEY", "MAP", "BYTERANGE", "ENDLIST", "PART", "STREAM-INF"):
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
