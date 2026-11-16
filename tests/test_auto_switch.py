"""面板上的「自动换源」开关。

背景：一个台挂着好几条线路时，中转层会在「上游拒了」「清单不往前滚」这两种
情况下自动换到下一条。抗封靠它，但两条线路都有问题时会来回跳——播放器那边
看到的就是画面一卡一卡。开关的作用是让用户能把这一步关掉，先把画面稳住，
也顺手用来定位坏掉的是哪条线路。

这里有三个层面要守住：配置能读能存、面板控件在「宽带与省份」页、关掉之后
中转层真的不换线路。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptvhd.settings import Settings, coerce_panel, load_settings

WEB = Path(__file__).resolve().parent.parent / "web"


class TestSettings(unittest.TestCase):
    def _settings(self, text: str = "") -> Settings:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "config.toml").write_text(text, encoding="utf-8")
        return load_settings(tmp)

    def test_on_by_default(self):
        self.assertTrue(self._settings("[relay]\n").auto_switch)

    def test_off_in_config(self):
        self.assertFalse(self._settings("[relay]\nauto_switch = false\n").auto_switch)

    def test_env_overrides(self):
        s = self._settings("[relay]\nauto_switch = true\n")
        with patch.dict("os.environ", {"HDIPTV_AUTO_SWITCH": "0"}):
            self.assertFalse(s.auto_switch)

    def test_panel_wins_over_config(self):
        s = self._settings("[relay]\nauto_switch = true\n")
        # 面板写在 panel.json 里，优先级最高：用户点了才算数
        s.panel["auto_switch"] = False
        self.assertFalse(s.auto_switch)

    def test_panel_wins_over_env(self):
        # 部署时写了 HDIPTV_AUTO_SWITCH=0 也不能压住面板：这个开关是给「正卡着
        # 看的人」用的，点了必须立刻见效，否则用户会认为开关坏了。
        s = self._settings("[relay]\nauto_switch = false\n")
        s.panel["auto_switch"] = True
        with patch.dict("os.environ", {"HDIPTV_AUTO_SWITCH": "0"}):
            self.assertTrue(s.auto_switch)


class TestCoerce(unittest.TestCase):
    def test_accepts_bool_like(self):
        self.assertFalse(coerce_panel({"auto_switch": "0"})["auto_switch"])
        self.assertFalse(coerce_panel({"auto_switch": False})["auto_switch"])
        self.assertTrue(coerce_panel({"auto_switch": "1"})["auto_switch"])
        self.assertTrue(coerce_panel({"auto_switch": "on"})["auto_switch"])

    def test_garbage_becomes_true(self):
        # 认不出来的值按「开」处理：这一项是安全网，默默关掉反而更糟
        self.assertTrue(coerce_panel({"auto_switch": None})["auto_switch"])
        self.assertTrue(coerce_panel({"auto_switch": "随便"})["auto_switch"])


class TestPanel(unittest.TestCase):
    def _region_block(self) -> str:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        start = html.index('id="regionPanel"')
        return html[start:html.index("</section>", start)]

    def test_control_in_region_view(self):
        self.assertIn('id="setAutoSwitch"', self._region_block())

    def test_has_on_off_options(self):
        block = self._region_block()
        start = block.index('id="setAutoSwitch"')
        select = block[start:start + 200]
        self.assertIn('<option value="1">', select)
        self.assertIn('<option value="0">', select)

    def test_explains_what_it_does(self):
        # 用户看不懂「自动换源」四个字就不会去点它，说明必须留在页面上
        block = self._region_block()
        self.assertIn("自动换源", block)
        self.assertIn("来回跳", block)

    def test_saved_with_region_settings(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('auto_switch:', js)              # 提交时带上
        self.assertIn('"setAutoSwitch"', js)           # 回填 + 变更标记
        self.assertIn('$("setAutoSwitch").value === "1"', js)


class TestRelayWiring(unittest.TestCase):
    """关掉开关之后，中转层不能再往下一条线路跳。"""

    def test_relay_receives_setting(self):
        from iptvhd.relay import Relay

        relay = Relay(lambda slug: ("http://a/1.m3u8", "ua"), warm=False, auto_switch=False)
        self.assertFalse(relay._auto_switch)
        relay.configure(auto_switch=True)
        self.assertTrue(relay._auto_switch)

    def test_configure_clears_stuck_streak(self):
        from iptvhd.relay import Relay

        relay = Relay(lambda slug: ("http://a/1.m3u8", "ua"), warm=False, auto_switch=False)
        relay._stuck_streak["c:x"] = 3
        relay.configure(auto_switch=True)
        self.assertEqual(relay._stuck_streak, {})

    def test_rotate_swallowed_when_off(self):
        from iptvhd.relay import Relay

        calls = []

        def resolver(slug, rotate: bool = False):
            calls.append(rotate)
            return "http://a/1.m3u8", "ua"

        relay = Relay(resolver, warm=False, auto_switch=False)
        relay.upstream("c:x", rotate=True)
        self.assertEqual(calls, [False])           # 换线被吞，退化成换票


if __name__ == "__main__":
    unittest.main()
