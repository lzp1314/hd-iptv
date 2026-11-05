"""从 TS 分片里读真实分辨率。

直播列表上标的「1080P」经常名不副实：来源本身就是 720p 上变换，或者平台
把 720p 的流也挂在 1080 档位下。光看地址或接口字段判断不了，只能看码流。

这里只做一件事：在 TS 分片里找到 H.264 / H.265 的 SPS，解出宽高。分片开头
几乎一定带一帧 IDR，SPS 就在它前面，所以取任一提前量的数据都能读到。

不依赖 ffmpeg，纯标准库。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Resolution:
    width: int = 0
    height: int = 0
    codec: str = ""

    @property
    def ok(self) -> bool:
        return self.width > 0 and self.height > 0


class _Bits:
    """按位读取，带 Exp-Golomb。"""

    __slots__ = ("data", "pos", "size")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.size = len(data) * 8

    def bit(self) -> int:
        if self.pos >= self.size:
            raise ValueError("SPS 越界")
        byte = self.data[self.pos >> 3]
        value = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def skip(self, count: int) -> None:
        self.pos += count
        if self.pos > self.size:
            raise ValueError("SPS 越界")

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("Exp-Golomb 太长")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _unescape(data: bytes) -> bytes:
    """去掉防竞争字节 0x03（00 00 03 → 00 00）。"""
    out = bytearray()
    i = 0
    size = len(data)
    while i < size:
        if i + 2 < size and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += data[i : i + 2]
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _find_nal(data: bytes, header_first: int, header_second: int | None = None) -> bytes:
    """找第一个匹配起始码 + NAL 头的负载。"""
    size = len(data)
    for i in range(size - 5):
        if data[i] or data[i + 1]:
            continue
        if data[i + 2] != 1:
            continue
        if data[i + 3] != header_first:
            continue
        if header_second is not None and data[i + 4] != header_second:
            continue
        start = i + 3 + (2 if header_second is not None else 1)
        end = data.find(b"\x00\x00\x01", start)
        if end < 0:
            end = min(size, start + 512)
        return data[start:end]
    return b""


def parse_h264_sps(sps: bytes) -> tuple[int, int]:
    """解析 H.264 SPS，返回 (宽, 高)。"""
    bits = _Bits(_unescape(sps))
    profile = bits.bits(8)
    bits.bits(8)                     # constraint flags
    bits.bits(8)                     # level_idc
    bits.ue()                        # seq_parameter_set_id

    chroma_format_idc = 1
    separate_colour_plane = 0
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = bits.ue()
        if chroma_format_idc == 3:
            separate_colour_plane = bits.bit()
        bits.ue()                    # bit_depth_luma_minus8
        bits.ue()                    # bit_depth_chroma_minus8
        bits.bit()                   # qpprime_y_zero_transform_bypass_flag
        if bits.bit():               # seq_scaling_matrix_present_flag
            count = 8 if chroma_format_idc != 3 else 12
            for idx in range(count):
                if not bits.bit():
                    continue
                last = 8 if idx < 6 else 12
                for _ in range(last):
                    if not bits.bit():
                        break
                    bits.se()

    bits.ue()                        # log2_max_frame_num_minus4
    poc_type = bits.ue()
    if poc_type == 0:
        bits.ue()                    # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        bits.bit()                   # delta_pic_order_always_zero_flag
        bits.se()
        bits.se()
        for _ in range(bits.ue()):
            bits.se()

    bits.ue()                        # max_num_ref_frames
    bits.bit()                       # gaps_in_frame_num_value_allowed_flag
    width_mbs = bits.ue() + 1
    height_units = bits.ue() + 1
    frame_mbs_only = bits.bit()
    if not frame_mbs_only:
        bits.bit()                   # mb_adaptive_frame_field_flag
    bits.bit()                       # direct_8x8_inference_flag

    crop_l = crop_r = crop_t = crop_b = 0
    if bits.bit():                   # frame_cropping_flag
        crop_l = bits.ue()
        crop_r = bits.ue()
        crop_t = bits.ue()
        crop_b = bits.ue()

    if chroma_format_idc == 0 or separate_colour_plane:
        crop_unit_x, crop_unit_y = 1, 2 - frame_mbs_only
    else:
        sub_width = 2 if chroma_format_idc in (1, 2) else 1
        sub_height = 2 if chroma_format_idc == 1 else 1
        crop_unit_x = sub_width
        crop_unit_y = sub_height * (2 - frame_mbs_only)

    width = width_mbs * 16 - (crop_l + crop_r) * crop_unit_x
    height = height_units * 16 * (2 - frame_mbs_only) - (crop_t + crop_b) * crop_unit_y
    return width, height


def parse_hevc_sps(sps: bytes) -> tuple[int, int]:
    """解析 H.265 (HEVC) SPS 里能直接读到的宽高。

    HEVC 的 SPS 字段多且受 profile_tier_level 影响，完整解析容易出错；
    宽高在 profile_tier_level 之后，这里是按结构顺序定位的。
    """
    bits = _Bits(_unescape(sps))
    bits.bits(4)                     # sps_video_parameter_set_id
    max_sub_layers = bits.bits(3)
    bits.bit()                       # sps_temporal_id_nesting_flag

    # profile_tier_level
    bits.bits(2 + 1 + 5)             # general_profile_space / tier / profile_idc
    bits.bits(32)                    # compatibility flags
    bits.bits(4 + 43 + 1)            # constraint flags
    bits.bits(8)                     # level_idc
    sub_profile = []
    sub_level = []
    for _ in range(max_sub_layers):
        sub_profile.append(bits.bit())
        sub_level.append(bits.bit())
    if max_sub_layers > 0:
        for _ in range(max_sub_layers, 8):
            bits.bits(2)
    for idx in range(max_sub_layers):
        if sub_profile[idx]:
            bits.bits(32 + 4 + 43 + 1)
        if sub_level[idx]:
            bits.bits(8)

    bits.ue()                        # sps_seq_parameter_set_id
    chroma_format_idc = bits.ue()
    if chroma_format_idc == 3:
        bits.bit()                   # separate_colour_plane_flag
    width = bits.ue()
    height = bits.ue()
    if bits.bit():                   # conformance_window_flag
        sub_width = 2 if chroma_format_idc in (1, 2) else 1
        sub_height = 2 if chroma_format_idc == 1 else 1
        left = bits.ue()
        right = bits.ue()
        top = bits.ue()
        bottom = bits.ue()
        width -= (left + right) * sub_width
        height -= (top + bottom) * sub_height
    return width, height


def _plausible(width: int, height: int) -> bool:
    return (
        320 <= width <= 7680
        and 180 <= height <= 4320
        and width % 2 == 0
        and height % 2 == 0
        and width >= height          # 直播不会是竖向
    )


def probe(data: bytes) -> Resolution:
    """从一段 TS / 裸流数据里读分辨率，读不到返回全 0。"""
    if not data:
        return Resolution()

    # H.264：NAL 头 0x67（type 7 = SPS），前面可能还有一个字节的 nal_ref_idc
    sps = _find_nal(data, 0x67)
    if sps:
        try:
            width, height = parse_h264_sps(sps)
            if _plausible(width, height):
                return Resolution(width, height, "h264")
        except (ValueError, IndexError):
            pass

    # H.265：NAL 头两字节是 0x42 0x01（type 33 = SPS）
    sps = _find_nal(data, 0x42, 0x01)
    if sps:
        try:
            width, height = parse_hevc_sps(sps)
            if _plausible(width, height):
                return Resolution(width, height, "h265")
        except (ValueError, IndexError):
            pass

    return Resolution()


__all__ = ["probe", "Resolution", "parse_h264_sps", "parse_hevc_sps"]
