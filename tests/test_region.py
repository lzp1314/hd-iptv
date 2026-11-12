"""宽带识别（省份 / 运营商 / IPv6）与「按名字跳过源」。

这两件事是给「不在湖北移动宽带上的人」用的：识别出来是为了在面板上说清
为什么只有几十个台，跳过官方源是为了不去白跑那 90 多条注定 403 的地址。

设计上刻意做成「识别失败什么都不影响」，所以这里的测试重点不是「认得多准」，
而是「认不出来时不许炸」。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iptvhd import region


class TestProvince(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(region.province_of("中国 湖北 武汉"), "hubei")
        self.assertEqual(region.province_of("湖北省武汉市"), "hubei")
        self.assertEqual(region.province_of("广东深圳"), "guangdong")

    def test_english(self):
        self.assertEqual(region.province_of("Hubei"), "hubei")
        self.assertEqual(region.province_of("Guangdong"), "guangdong")
        self.assertEqual(region.province_of("Zhejiang, China"), "zhejiang")

    def test_special_spellings(self):
        # 各家 IP 库对这几个自治区的写法最不统一，单独盯一下
        self.assertEqual(region.province_of("Nei Mongol"), "neimenggu")
        self.assertEqual(region.province_of("Inner Mongolia"), "neimenggu")
        self.assertEqual(region.province_of("Tibet"), "xizang")
        self.assertEqual(region.province_of("Xinjiang Uygur"), "xinjiang")

    def test_unknown(self):
        self.assertEqual(region.province_of("美国 加利福尼亚"), "")
        self.assertEqual(region.province_of(""), "")

    def test_codes_cover_all_31(self):
        # 拼音代码要和公开源目录名对得上，少一个以后自动选源就拼不出路径
        self.assertEqual(len(region.PROVINCES), 31)
        for code in ("hubei", "beijing", "neimenggu", "xizang", "xinjiang"):
            self.assertIn(code, region.PROVINCES)


class TestIsp(unittest.TestCase):
    def test_chinese(self):
        for text in ("移动", "中国移动", "中国移动通信"):
            self.assertEqual(region.isp_of(text), "mobile")
        self.assertEqual(region.isp_of("中国电信"), "telecom")
        self.assertEqual(region.isp_of("中国联通"), "unicom")
        self.assertEqual(region.isp_of("中国广电"), "broadnet")

    def test_as_org(self):
        self.assertEqual(
            region.isp_of("AS9808 China Mobile Communications Group Co., Ltd."), "mobile"
        )
        self.assertEqual(region.isp_of("AS4134 Chinanet"), "telecom")
        self.assertEqual(region.isp_of("AS4837 China Unicom"), "unicom")

    def test_unknown(self):
        self.assertEqual(region.isp_of("清华大学"), "")
        self.assertEqual(region.isp_of(""), "")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def detect(self, **kw):
        """统一把 IPv6 探测钉死，避免测试去连公网。"""
        kw.setdefault("timeout", 1.0)
        with mock.patch.object(region, "ipv6_available", return_value=True):
            return region.detect(self.dir, **kw)


class TestDetect(_Tmp):
    def test_uses_first_source(self):
        with mock.patch.object(region, "_by_ipip", return_value={
            "ip": "2409:8a4d::1", "province": "hubei", "isp": "mobile",
            "source": "ipip", "raw": "x",
        }):
            info = self.detect()
        self.assertTrue(info["ok"])
        self.assertEqual(info["province_name"], "湖北")
        self.assertEqual(info["isp_name"], "中国移动")
        self.assertTrue(info["ipv6"])
        self.assertEqual(info["source"], "ipip")

    def test_falls_back_when_first_fails(self):
        # 国内访问境外接口、或反过来，单点失败很常见，必须能换源
        with mock.patch.object(region, "_by_ipip", side_effect=OSError("boom")), \
             mock.patch.object(region, "_by_ipinfo", return_value={
                 "ip": "117.1.1.1", "province": "guangdong", "isp": "telecom",
                 "source": "ipinfo", "raw": "x",
             }):
            info = self.detect()
        self.assertTrue(info["ok"])
        self.assertEqual(info["province_name"], "广东")
        self.assertEqual(info["source"], "ipinfo")

    def test_all_sources_fail_is_not_an_error(self):
        with mock.patch.object(region, "_by_ipip", side_effect=OSError("x")), \
             mock.patch.object(region, "_by_ipinfo", side_effect=ValueError("y")):
            info = self.detect()
        self.assertFalse(info["ok"])
        self.assertEqual(info["province"], "")
        self.assertIn("没能识别", info["note"])
        # 失败也照样给一句人能看懂的话，面板直接显示，不用前端再拼
        self.assertTrue(info["note"])

    def test_manual_overrides_detection(self):
        with mock.patch.object(region, "_by_ipip") as fake:
            info = self.detect(manual_province="hunan", manual_isp="telecom")
        fake.assert_not_called()          # 手动指定就不该再问网络
        self.assertTrue(info["manual"])
        self.assertEqual(info["province_name"], "湖南")
        self.assertEqual(info["isp_name"], "中国电信")
        self.assertEqual(info["source"], "manual")

    def test_manual_accepts_chinese(self):
        info = self.detect(manual_province="湖北", manual_isp="移动")
        self.assertEqual(info["province"], "hubei")
        self.assertEqual(info["isp"], "mobile")

    def test_bad_manual_value_is_ignored(self):
        # 写错一个字符不该让人彻底没结果：忽略这个值，照常自动识别。
        # 这里让自动识别也失败，确认那个错值没有混进结果。
        with mock.patch.object(region, "_by_ipip", side_effect=OSError("x")), \
             mock.patch.object(region, "_by_ipinfo", side_effect=OSError("y")):
            info = self.detect(manual_province="火星")
        self.assertEqual(info["province"], "")
        self.assertNotIn("manual", info)

    def test_cache_reused(self):
        with mock.patch.object(region, "_by_ipip", return_value={
            "ip": "1.1.1.1", "province": "hubei", "isp": "mobile",
            "source": "ipip", "raw": "x",
        }):
            self.detect()
        # 第二次不该再请求网络
        with mock.patch.object(region, "_by_ipip", side_effect=AssertionError("不该联网")), \
             mock.patch.object(region, "_by_ipinfo", side_effect=AssertionError("不该联网")):
            info = self.detect()
        self.assertEqual(info["province_name"], "湖北")
        self.assertEqual(info["source"], "cache")

    def test_force_ignores_cache(self):
        with mock.patch.object(region, "_by_ipip", return_value={
            "ip": "1.1.1.1", "province": "hubei", "isp": "mobile",
            "source": "ipip", "raw": "x",
        }):
            self.detect()
        with mock.patch.object(region, "_by_ipip", return_value={
            "ip": "2.2.2.2", "province": "sichuan", "isp": "telecom",
            "source": "ipip", "raw": "y",
        }):
            info = self.detect(force=True)
        self.assertEqual(info["province_name"], "四川")

    def test_expired_cache_is_ignored(self):
        path = self.dir / region.CACHE_NAME
        path.write_text(json.dumps({"province": "hubei", "at": 1.0}), encoding="utf-8")
        with mock.patch.object(region, "_by_ipip", return_value={
            "ip": "", "province": "hunan", "isp": "unicom", "source": "ipip", "raw": "z",
        }):
            info = self.detect()
        self.assertEqual(info["province_name"], "湖南")

    def test_broken_cache_file(self):
        (self.dir / region.CACHE_NAME).write_text("not json", encoding="utf-8")
        with mock.patch.object(region, "_by_ipip", return_value={
            "ip": "", "province": "hubei", "isp": "mobile", "source": "ipip", "raw": "x",
        }):
            info = self.detect()
        self.assertTrue(info["ok"])

    def test_note_says_no_ipv6(self):
        with mock.patch.object(region, "ipv6_available", return_value=False), \
             mock.patch.object(region, "_by_ipip", return_value={
                 "ip": "1.1.1.1", "province": "hubei", "isp": "mobile",
                 "source": "ipip", "raw": "x",
             }):
            info = region.detect(self.dir, timeout=1.0)
        self.assertIn("无 IPv6", info["note"])


class TestSkipSources(unittest.TestCase):
    """HDIPTV_SKIP_SOURCES：按名字整条跳过源。"""

    def setUp(self):
        from iptvhd.settings import load_settings
        self.settings = load_settings(".")
        self._old = os.environ.pop("HDIPTV_SKIP_SOURCES", None)

    def tearDown(self):
        os.environ.pop("HDIPTV_SKIP_SOURCES", None)
        if self._old is not None:
            os.environ["HDIPTV_SKIP_SOURCES"] = self._old

    def names(self):
        from iptvhd.pipeline import load_sources
        return [x.get("name") for x in load_sources(self.settings)]

    def test_default_keeps_everything(self):
        self.assertIn("运营商官方源", self.names())

    def test_skip_by_name(self):
        os.environ["HDIPTV_SKIP_SOURCES"] = "运营商官方源"
        self.assertNotIn("运营商官方源", self.names())

    def test_skip_multiple_and_fullwidth_comma(self):
        os.environ["HDIPTV_SKIP_SOURCES"] = "运营商官方源，移动IPTV-本省专用"
        names = self.names()
        self.assertNotIn("运营商官方源", names)
        self.assertNotIn("移动IPTV-本省专用", names)

    def test_unknown_name_is_harmless(self):
        os.environ["HDIPTV_SKIP_SOURCES"] = "根本不存在的源"
        self.assertIn("运营商官方源", self.names())

    def test_empty_string_skips_nothing(self):
        os.environ["HDIPTV_SKIP_SOURCES"] = "  "
        self.assertIn("运营商官方源", self.names())


if __name__ == "__main__":
    unittest.main()
