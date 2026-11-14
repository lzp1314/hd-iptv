"""面板手动选省份 / 运营商：值收敛、优先级、批量扫描。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptvhd.ispcheck import scan_province
from iptvhd.settings import Settings, coerce_panel


class TestCoerceRegionIsp(unittest.TestCase):
    def test_accepts_pinyin_and_chinese(self):
        self.assertEqual(coerce_panel({"region": "hubei"})["region"], "hubei")
        self.assertEqual(coerce_panel({"region": "湖北省"})["region"], "hubei")
        self.assertEqual(coerce_panel({"isp": "mobile"})["isp"], "mobile")
        self.assertEqual(coerce_panel({"isp": "中国移动"})["isp"], "mobile")

    def test_blank_means_auto(self):
        self.assertEqual(coerce_panel({"region": ""})["region"], "")
        self.assertEqual(coerce_panel({"isp": "  "})["isp"], "")

    def test_invalid_dropped(self):
        out = coerce_panel({"region": "火星", "isp": "铁通"})
        self.assertNotIn("region", out)
        self.assertNotIn("isp", out)


class TestHintPriority(unittest.TestCase):
    def _settings(self, **kw) -> Settings:
        return Settings(root=Path(tempfile.mkdtemp()), **kw)

    def test_panel_beats_env(self):
        s = self._settings(panel={"region": "anhui", "isp": "mobile"})
        with patch.dict("os.environ", {"HDIPTV_REGION": "hubei", "HDIPTV_ISP": "telecom"}):
            self.assertEqual(s.region_hint, "anhui")
            self.assertEqual(s.isp_hint, "mobile")

    def test_blank_panel_overrides_env(self):
        # 面板里主动选回「自动识别」（空串）时，不能被环境变量顶回去
        s = self._settings(panel={"region": "", "isp": ""})
        with patch.dict("os.environ", {"HDIPTV_REGION": "hubei", "HDIPTV_ISP": "mobile"}):
            self.assertEqual(s.region_hint, "")
            self.assertEqual(s.isp_hint, "")

    def test_env_when_panel_absent(self):
        s = self._settings()
        with patch.dict("os.environ", {"HDIPTV_REGION": "hubei", "HDIPTV_ISP": "mobile"}):
            self.assertEqual(s.region_hint, "hubei")
            self.assertEqual(s.isp_hint, "mobile")

    def test_config_fallback(self):
        s = self._settings(data={"output": {"region": "hunan", "isp": "telecom"}})
        with patch.dict("os.environ", {"HDIPTV_REGION": "", "HDIPTV_ISP": ""}):
            self.assertEqual(s.region_hint, "hunan")
            self.assertEqual(s.isp_hint, "telecom")


class TestScanProvince(unittest.TestCase):
    def test_counts_public_entries(self):
        text = (
            "#EXTM3U\n"
            "#EXTINF:-1,湖南卫视\nhttp://1.2.3.4/live/hn.m3u8\n"
            "#EXTINF:-1,CCTV1\nhttp://5.6.7.8/cctv1.m3u8\n"
            "#EXTINF:-1,内网\nhttp://192.168.1.2/x.m3u8\n"
        )

        def fake(province, isp, timeout=0):
            if isp == "mobile":
                return text, "ok"
            return "", "missing"

        with patch("iptvhd.ispcheck.fetch_list", side_effect=fake):
            got = scan_province("hunan", timeout=1.0)
        self.assertEqual(got["province"], "hunan")
        self.assertEqual(got["isps"]["mobile"]["state"], "ok")
        # 内网那条要被过滤掉，只数公网入口
        self.assertEqual(got["isps"]["mobile"]["entries"], 2)
        self.assertEqual(got["isps"]["telecom"]["state"], "missing")
        self.assertEqual(got["isps"]["telecom"]["entries"], 0)

    def test_empty_province(self):
        got = scan_province("")
        self.assertEqual(got["isps"], {})
        self.assertIn("error", got)


if __name__ == "__main__":
    unittest.main()
