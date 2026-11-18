"""频道展示顺序：央视 → 卫视 → 其它，CCTV 数字自然序。"""

import unittest

from iptvhd.models import Channel, Stream
from iptvhd.normalize import channel_display_key, parse_channels
from iptvhd.pipeline import _module_channels


TEMPLATE = parse_channels(
    "央视,#genre#\n"
    "CCTV-1\nCCTV-2\nCCTV-5\nCCTV-10\nCCTV-13\n"
    "CGTN 纪录\n"
    "卫视,#genre#\n"
    "湖南卫视\n浙江卫视\n"
)


def _stream(name, source="运营商官方源"):
    return Stream(url="http://cdn/x.m3u8", raw_name=name, source=source,
                  channel=name, ok=True)


class TestChannelDisplayKey(unittest.TestCase):
    def test_cctv_numbers_are_natural_not_lexical(self):
        key = channel_display_key(TEMPLATE)
        names = ["CCTV-10", "CCTV-2", "CCTV-1", "CCTV-13", "CCTV-5"]
        self.assertEqual(sorted(names, key=key),
                         ["CCTV-1", "CCTV-2", "CCTV-5", "CCTV-10", "CCTV-13"])

    def test_cctv_before_wei_shi_before_others(self):
        key = channel_display_key(TEMPLATE)
        names = ["苏州新闻综合", "湖南卫视", "CCTV-1", "CGTN 纪录", "浙江卫视"]
        ordered = sorted(names, key=key)
        # CGTN 含 CCTV 词根？不含，但 normalize 后是 cgtn，属其它；央视组仅 cctv/央视
        self.assertLess(ordered.index("CCTV-1"), ordered.index("湖南卫视"))
        self.assertLess(ordered.index("湖南卫视"), ordered.index("苏州新闻综合"))
        self.assertLess(ordered.index("CCTV-1"), ordered.index("CGTN 纪录"))

    def test_template_order_within_category(self):
        key = channel_display_key(TEMPLATE)
        # 模板里 CCTV-5 在 CCTV-10 前，数字序也一致；换一组模板顺序验证行号生效
        tpl = parse_channels("央视,#genre#\nCCTV-5\nCCTV-1\nCCTV-2\n")
        ordered = sorted(["CCTV-1", "CCTV-2", "CCTV-5"], key=channel_display_key(tpl))
        self.assertEqual(ordered, ["CCTV-5", "CCTV-1", "CCTV-2"])

    def test_no_template_still_groups_and_numbers(self):
        key = channel_display_key(None)
        names = ["湖南卫视", "CCTV-10", "CCTV-1", "苏州台"]
        ordered = sorted(names, key=key)
        self.assertEqual(ordered[0], "CCTV-1")
        self.assertLess(ordered.index("CCTV-10"), ordered.index("湖南卫视"))
        self.assertLess(ordered.index("湖南卫视"), ordered.index("苏州台"))

    def test_plain_before_plus_variant(self):
        # 真实咪咕名带中文后缀：CCTV5体育 要排在 CCTV5+体育赛事 前
        key = channel_display_key(None)
        names = ["CCTV5+体育赛事", "CCTV5体育", "CCTV4欧洲", "CCTV4中文国际", "CCTV4美洲"]
        ordered = sorted(names, key=key)
        self.assertEqual(ordered, ["CCTV4中文国际", "CCTV4欧洲", "CCTV4美洲",
                                   "CCTV5体育", "CCTV5+体育赛事"])


class TestModuleChannelsOrder(unittest.TestCase):
    def test_module_channels_sorted_by_display_key(self):
        streams = [
            _stream("CCTV-10"), _stream("CCTV-1"), _stream("湖南卫视"),
            _stream("苏州新闻综合"), _stream("CCTV-5"), _stream("浙江卫视"),
        ]
        rows = _module_channels(streams, 3, lambda _: None, template=TEMPLATE)
        names = [c.name for c in rows]
        self.assertEqual(names, ["CCTV-1", "CCTV-5", "CCTV-10",
                                 "湖南卫视", "浙江卫视", "苏州新闻综合"])


if __name__ == "__main__":
    unittest.main()
