"""码流分辨率解析测试。

样本一：真实央视 1080P 分片里的 SPS（十六进制），确认能读对。
样本二：按 H.264 语法手工拼出的 720P SPS，确认低分辨率不会被误读成 1080P。
"""

from __future__ import annotations

import unittest

from iptvhd import videoinfo
from iptvhd.models import height_grade

# 来自一路 1080P 直播分片，NAL 头 0x67 之后的 SPS 负载
REAL_1080P_SPS = bytes.fromhex(
    "67640029acd100780227e59a2000000300200000065d5800ea60030d5259803c60c44800"
)


def _ue(value: int) -> str:
    """Exp-Golomb 编码，返回比特串。"""
    code = bin(value + 1)[2:]
    return "0" * (len(code) - 1) + code


def _bits_to_bytes(bits: str) -> bytes:
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))


def make_sps(width_mbs: int, height_units: int) -> bytes:
    """拼一个 baseline profile、无裁剪的 SPS 负载（不含 NAL 头）。"""
    bits = ""
    bits += format(66, "08b")          # profile_idc = baseline
    bits += "0" * 8                    # constraint flags
    bits += format(31, "08b")          # level_idc
    bits += _ue(0)                     # seq_parameter_set_id
    bits += _ue(0)                     # log2_max_frame_num_minus4
    bits += _ue(2)                     # pic_order_cnt_type = 2
    bits += _ue(1)                     # max_num_ref_frames
    bits += "0"                        # gaps_in_frame_num_value_allowed_flag
    bits += _ue(width_mbs - 1)         # pic_width_in_mbs_minus1
    bits += _ue(height_units - 1)      # pic_height_in_map_units_minus1
    bits += "1"                        # frame_mbs_only_flag
    bits += "1"                        # direct_8x8_inference_flag
    bits += "0"                        # frame_cropping_flag
    bits += "0"                        # vui_parameters_present_flag
    return _bits_to_bytes(bits)


def wrap_ts(sps_payload: bytes, head: bytes = b"", tail: bytes = b"") -> bytes:
    """把 SPS 包成「起始码 + NAL 头 + 负载」的形式，再塞点无关字节。"""
    return head + b"\x00\x00\x00\x01\x67" + sps_payload + b"\x00\x00\x00\x01\x68" + tail


class RealSampleTest(unittest.TestCase):
    def test_reads_1080p_from_real_sps(self):
        width, height = videoinfo.parse_h264_sps(REAL_1080P_SPS[1:])
        self.assertEqual((width, height), (1920, 1080))

    def test_probe_finds_sps_inside_junk(self):
        data = wrap_ts(REAL_1080P_SPS[1:], head=b"\x47" * 300, tail=b"\x47" * 200)
        found = videoinfo.probe(data)
        self.assertTrue(found.ok)
        self.assertEqual((found.width, found.height), (1920, 1080))
        self.assertEqual(found.codec, "h264")


class SyntheticTest(unittest.TestCase):
    def test_reads_720p(self):
        sps = make_sps(80, 45)   # 80*16 = 1280，45*16 = 720
        width, height = videoinfo.parse_h264_sps(sps)
        self.assertEqual((width, height), (1280, 720))

    def test_reads_4k(self):
        sps = make_sps(240, 135)  # 240*16 = 3840，135*16 = 2160
        width, height = videoinfo.parse_h264_sps(sps)
        self.assertEqual((width, height), (3840, 2160))

    def test_720p_not_mistaken_for_1080p(self):
        found = videoinfo.probe(wrap_ts(make_sps(80, 45)))
        self.assertEqual(found.height, 720)
        self.assertEqual(height_grade(found.height), "720P")


class RobustnessTest(unittest.TestCase):
    def test_empty_and_garbage_return_zero(self):
        self.assertFalse(videoinfo.probe(b"").ok)
        self.assertFalse(videoinfo.probe(b"\x47" * 4096).ok)
        # 假的起始码 + 垃圾负载，不能崩
        self.assertFalse(videoinfo.probe(b"\x00\x00\x01\x67\xff\xff\xff").ok)

    def test_unescape_removes_emulation_bytes(self):
        self.assertEqual(videoinfo._unescape(b"\x00\x00\x03\x01"), b"\x00\x00\x01")
        self.assertEqual(videoinfo._unescape(b"\x01\x02"), b"\x01\x02")

    def test_vertical_resolution_rejected(self):
        """竖屏（宽 < 高）不会是直播信号，判为解析失败。"""
        found = videoinfo.probe(wrap_ts(make_sps(45, 80)))
        self.assertFalse(found.ok)


if __name__ == "__main__":
    unittest.main()
