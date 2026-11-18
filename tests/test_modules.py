"""数据源模块化：每个源独立成组，开关同时控制采集与订阅输出。

要点：
- 面板关掉的模块不参与采集，但仍在清单里（能重新打开）；
- 订阅按请求现拼时按同样的名字整组摘掉，不用等下一轮更新；
- 同一个台在不同模块里各用一个频道级地址，换线不串组。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from iptvhd.models import Channel, Stream
from iptvhd.pipeline import load_sources
from iptvhd.settings import Settings, coerce_panel, load_settings

WEB = Path(__file__).resolve().parent.parent / "web"


def project(disabled: list[str] | None = None) -> Settings:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config").mkdir()
    (tmp / "config" / "sources.toml").write_text(
        '[[source]]\nname = "源A"\ntype = "local"\npath = "a.txt"\nenabled = true\n'
        '[[source]]\nname = "源B"\ntype = "subscription"\nurl = "http://127.0.0.1/x"\nenabled = true\n',
        encoding="utf-8",
    )
    if disabled is not None:
        (tmp / "config" / "panel.json").write_text(
            json.dumps({"disabled_modules": disabled}), encoding="utf-8"
        )
    return load_settings(tmp)


class TestModuleSettings(unittest.TestCase):
    def test_panel_value_is_cleaned(self):
        out = coerce_panel({"disabled_modules": ["源B", " 源A ", "源B", "", 42]})
        self.assertEqual(out["disabled_modules"], ["源A", "源B"])
        # 逗号串也能收，前端两种提交形态都认
        self.assertEqual(
            coerce_panel({"disabled_modules": "源A,源B"})["disabled_modules"],
            ["源A", "源B"],
        )

    def test_module_disabled_matches_exact_name(self):
        s = project(["源B"])
        self.assertTrue(s.module_disabled("源B"))
        self.assertFalse(s.module_disabled("源A"))
        # 前缀不能误伤：名字相近的两个源是两回事
        self.assertFalse(s.module_disabled("源B备用"))
        self.assertFalse(project().module_disabled("源B"))

    def test_disabled_source_is_kept_but_not_collected(self):
        s = project(["源B"])
        sources = load_sources(s)
        self.assertEqual([x["name"] for x in sources if x["enabled"]], ["源A"])
        # 关掉的仍在清单里，面板才能列出并重新打开
        self.assertEqual({x["name"] for x in sources}, {"源A", "源B"})

    def test_catalog_lists_disabled_module_as_off(self):
        from iptvhd.service import _module_catalog

        rows = {x["name"]: x for x in _module_catalog(project(["源B"]))}
        self.assertEqual(set(rows), {"源A", "源B"})
        self.assertTrue(rows["源A"]["enabled"])
        self.assertFalse(rows["源B"]["enabled"])


class TestSubscriptionFilter(unittest.TestCase):
    def test_disabled_module_disappears_from_subscription(self):
        from iptvhd.service import _module_visible

        channels = [
            Channel(name="CCTV-1", group="咪咕视频", streams=[
                Stream(url="", slug="m:608807420", ok=True),
            ]),
            Channel(name="CCTV-1", group="运营商官方源", streams=[
                Stream(url="http://[2409:abc]/live.m3u8", ok=True),
            ]),
        ]
        kept = _module_visible(channels, project(["咪咕视频"]))
        self.assertEqual([c.group for c in kept], ["运营商官方源"])
        # 没关任何模块时一个都不动
        self.assertEqual(len(_module_visible(channels, project())), 2)

    def test_channel_address_is_unique_per_module(self):
        from iptvhd import writers
        from iptvhd.relay import channel_name, channel_slug

        slug_a = channel_slug("CCTV-1", "咪咕视频")
        slug_b = channel_slug("CCTV-1", "运营商官方源")
        self.assertNotEqual(slug_a, slug_b)
        # 展示名仍只是频道名，模块前缀不该漏到播放器看到的标题里
        self.assertEqual(channel_name(slug_a), "CCTV-1")
        self.assertEqual(channel_name(slug_b), "CCTV-1")

        def ch(group: str) -> Channel:
            return Channel(name="CCTV-1", group=group, streams=[
                Stream(url="http://up/a.m3u8", ok=True),
                Stream(url="http://up/b.m3u8", ok=True),
            ])

        text, _ = writers.render_m3u([ch("咪咕视频"), ch("运营商官方源")], relay_base="http://h")
        self.assertIn(f"/live/{slug_a}.m3u8", text)
        self.assertIn(f"/live/{slug_b}.m3u8", text)


class TestPanel(unittest.TestCase):
    def test_module_view_and_controls_exist(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="modules"', html)
        for eid in ("modulePanel", "moduleList", "moduleStatus", "moduleNote", "btnModuleSave"):
            self.assertIn(f'id="{eid}"', html)
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('btnId === "btnModuleSave"', js)
        self.assertIn("payload.disabled_modules", js)


if __name__ == "__main__":
    unittest.main()
