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


class TestPanelMarkup(unittest.TestCase):
    """面板 HTML 里必须真有这些控件。

    之前出过一次：后端接口、前端 JS 都写好了，HTML 里却没放下拉框，JS 用
    ``if ($("setRegion"))`` 保护着，于是悄无声息地什么都不发生——点按钮只提示
    「先选好省份和运营商」。这条测试就是拦这种「只缺一块拼图」的回归。
    """

    WEB = Path(__file__).resolve().parent.parent / "web"

    def test_region_controls_present(self):
        html = (self.WEB / "index.html").read_text(encoding="utf-8")
        for elem_id in ("setRegion", "setIsp", "setAutoRegion", "btnProbe", "btnScan"):
            self.assertIn(f'id="{elem_id}"', html, f"面板缺少 #{elem_id}")

    def test_province_select_has_auto_option(self):
        html = (self.WEB / "index.html").read_text(encoding="utf-8")
        start = html.index('id="setRegion"')
        block = html[start:start + 200]
        self.assertIn('<option value="">', block)

    def test_every_nav_item_has_view(self):
        html = (self.WEB / "index.html").read_text(encoding="utf-8")
        import re

        navs = re.findall(r'data-view="(\w+)"', html)
        views = set(re.findall(r'class="panel view" id="\w+" data-view="(\w+)"', html))
        self.assertTrue(navs)
        self.assertEqual(set(navs) - views, set(), "有导航项没有对应分区")


if __name__ == "__main__":
    unittest.main()
