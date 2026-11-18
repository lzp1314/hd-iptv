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

        catalog = _module_catalog(project(["源B"]))
        rows = {x["name"]: x for x in catalog["others"]}
        self.assertEqual(set(rows), {"源A", "源B"})
        self.assertTrue(rows["源A"]["enabled"])
        self.assertFalse(rows["源B"]["enabled"])

    def test_builtin_module_members_are_folded_into_one_switch(self):
        from iptvhd.settings import BUILTIN_MODULES, display_module, module_members

        # 官方直连与运营商官方源归到同一个展示模块
        self.assertEqual(display_module("官方直连"), "运营商官方源")
        self.assertEqual(display_module("运营商官方源"), "运营商官方源")
        # 三个聚合源、央视频两个源同理
        self.assertEqual(display_module("聚合源-精简线路"), "第三方聚合源")
        self.assertEqual(display_module("央视官方"), "央视频")
        self.assertEqual(display_module("咪咕视频"), "咪咕视频")
        # 非内置源原样返回
        self.assertEqual(display_module("浙江广电"), "浙江广电")
        self.assertIn("官方直连", module_members("运营商官方源"))
        self.assertEqual(set(BUILTIN_MODULES), {
            "运营商官方源", "第三方聚合源", "央视频", "咪咕视频",
        })

    def test_catalog_exposes_four_builtins_and_folds_members(self):
        from iptvhd.service import _module_catalog

        # 最小工程没有内置模块的底层源，内置卡片仍要列出（关着），
        # 脚本里注入的源按展示名归组，不重复出现在「更多源」。
        catalog = _module_catalog(project())
        self.assertEqual(
            [b["name"] for b in catalog["builtins"]],
            ["运营商官方源", "第三方聚合源", "央视频", "咪咕视频"],
        )
        names = {o["name"] for o in catalog["others"]}
        self.assertNotIn("源A", {m for b in catalog["builtins"] for m in b["members"]})

    def test_builtin_switch_disables_all_its_members(self):
        # 关掉展示模块名，它下面所有底层源在采集层都算关
        s = project(["运营商官方源"])
        self.assertTrue(s.module_disabled("官方直连"))
        self.assertTrue(s.module_disabled("运营商官方源"))
        self.assertFalse(s.module_disabled("聚合源-多线路"))
        # 旧配置只写了底层名，归一后同样命中展示模块
        s2 = project(["官方直连"])
        self.assertTrue(s2.module_disabled("运营商官方源"))


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

    def test_module_card_has_status_channels_and_config_button(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("builtin-status", js)
        self.assertIn("builtin-chip", js)
        self.assertIn('data-config=', js)
        self.assertIn("openModuleModal", js)

    def test_module_modal_and_channel_list_exist(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for eid in ("moduleModal", "moduleChannels", "moduleModalTitle", "btnModuleModalClose"):
            self.assertIn(f'id="{eid}"', html)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn(".module-modal", css)
        self.assertIn(".chan-chip", css)
        # display:flex 会盖掉浏览器默认的 [hidden]{display:none}，
        # 必须有显式规则，否则点「关闭」设了 hidden 却仍显示。
        self.assertIn(".module-modal[hidden]", css)
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("modal.hidden = true", js)

    def test_datasource_layout_full_width_with_inline_subscribe(self):
        """数据源页全屏铺开，订阅地址常驻右侧，与原订阅页是同一批地址。"""
        html = (WEB / "index.html").read_text(encoding="utf-8")
        # 数据源页有左右分栏：模块区 + 订阅栏
        self.assertIn('class="module-layout"', html)
        self.assertIn('class="module-main"', html)
        self.assertIn('class="subscribe-aside"', html)
        for eid in ("subUrlM3u", "subUrlTxt", "subUrlEpg", "subUrlJson", "subEpgNote"):
            self.assertIn(f'id="{eid}"', html)
        # 复制按钮指向新地址栏
        for cid in ("subUrlM3u", "subUrlTxt", "subUrlEpg", "subUrlJson"):
            self.assertIn(f'data-copy="{cid}"', html)

        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn(".module-layout", css)
        self.assertIn(".subscribe-aside", css)
        # 内容区不再限宽 1280：全屏铺开
        self.assertNotIn("max-width: 1280px", css)

        js = (WEB / "app.js").read_text(encoding="utf-8")
        # 同一批地址填两处：原订阅页 + 数据源页内联栏
        for eid in ("subUrlM3u", "subUrlTxt", "subUrlEpg", "subUrlJson"):
            self.assertIn(f'{eid}:', js)
        self.assertIn('$("subEpgNote")', js)


class TestModuleChannelSummary(unittest.TestCase):
    def test_channels_grouped_by_module_and_category(self):
        from iptvhd.service import _module_channels

        result = {"channels": [
            {"name": "CCTV-1 综合", "group": "央视频"},
            {"name": "湖南卫视", "group": "综合频道"},
            {"name": "CCTV-5 体育", "group": "咪咕视频"},
            {"name": "湖北卫视", "group": "运营商官方源"},
            {"name": "电影频道", "group": "咪咕视频"},
        ]}
        found = _module_channels(result)
        # 底层源名「综合频道」也是央视频的一员，归并到同一模块
        cctv = found["央视频"]
        self.assertEqual(cctv["total"], 2)
        self.assertEqual(cctv["groups"]["央视"], ["CCTV-1 综合"])
        self.assertEqual(cctv["groups"]["卫视"], ["湖南卫视"])
        self.assertEqual(found["咪咕视频"]["groups"]["央视"], ["CCTV-5 体育"])
        self.assertEqual(found["咪咕视频"]["groups"]["其它"], ["电影频道"])

    def test_channel_summary_survives_bad_result(self):
        from iptvhd.service import _module_channels, _module_catalog

        self.assertEqual(_module_channels(None), {})
        self.assertEqual(_module_channels({"channels": "坏数据"}), {})
        # 传了坏 result 的 catalog 仍要给四张卡片，字段齐全
        catalog = _module_catalog(project(), {"channels": "坏数据"})
        for m in catalog["builtins"]:
            self.assertEqual(m["channels"], {})
            self.assertEqual(m["count"], 0)

    def test_catalog_exposes_channel_fields(self):
        from iptvhd.service import _module_catalog

        result = {
            "channels": [{"name": "CCTV-1", "group": "央视频"}],
            "report": {"sources": [
                {"name": "综合频道", "count": 61},
                {"name": "咪咕视频", "count": 127},
            ]},
        }
        rows = {m["name"]: m for m in _module_catalog(project(), result)["builtins"]}
        self.assertEqual(rows["央视频"]["count"], 1)
        self.assertEqual(rows["央视频"]["collected"], 61)
        self.assertTrue(rows["央视频"]["configurable"])
        self.assertTrue(rows["咪咕视频"]["configurable"])
        self.assertFalse(rows["第三方聚合源"]["configurable"])


if __name__ == "__main__":
    unittest.main()
