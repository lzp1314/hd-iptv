"""少儿频道派生视图：只从已有频道里筛少儿台，不落盘、不改原始数据。

要点：
- 官方少儿台（CCTV-14 / 卡酷 / 各省少儿 / 卡通卫视）命中 official；
- 第三方动画轮播（海绵宝宝等）命中 cartoon，可单独关；
- 普通台（新闻/卫视/综合）不命中；
- 派生返回的仍是原 Channel 对象，group 不变——线路表按 group 建索引，
  改了会让 /live/c:... 找不到线路；
- M3U 显示分组名可覆盖成「少儿频道」，但播放地址里的 group 编码不变；
- 开关默认开，panel / env / config 三级来源，关掉后动画轮播可独立关。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iptvhd.collectors import SubscriptionCollector
from iptvhd.generators import KidsCollector
from iptvhd.kids import KIDS_MODULE, classify, kids_channels
from iptvhd.models import Channel, Stream
from iptvhd.net import HttpClient
from iptvhd.service import _kids_visible, _module_visible
from iptvhd.settings import Settings, coerce_panel, load_settings
from iptvhd.writers import render_m3u


def ch(name: str, group: str = "第三方聚合源") -> Channel:
    return Channel(
        name=name,
        group=group,
        streams=[
            Stream(url="http://cdn/a.m3u8", slug="s1", height=720),
            Stream(url="http://cdn/b.m3u8", slug="s2", height=1080),
        ],
    )


class ClassifyTest(unittest.TestCase):
    def test_official_school_channels(self):
        for name in (
            "CCTV-14", "CCTV-14 少儿", "CCTV14少儿", "北京卡酷少儿",
            "卡酷动画", "金鹰卡通", "优漫卡通", "嘉佳卡通",
            "海南少儿", "浙江少儿", "福建少儿", "辽宁教育青少",
            "经典动画大集合", "动漫秀场",
        ):
            self.assertEqual(classify(name), "official", name)

    def test_cartoon_replays(self):
        for name in ("海绵宝宝", "猫和老鼠", "火影忍者", "蜡笔小新", "七龙珠", "中华小当家", "漫画解说"):
            self.assertEqual(classify(name), "cartoon", name)

    def test_regular_channels_not_matched(self):
        for name in ("CCTV-1 综合", "湖南卫视", "北京卫视", "CCTV-5 体育", "浙江卫视"):
            self.assertEqual(classify(name), "", name)

    def test_normalization_cctv14_variants(self):
        # 连字符/空格/大小写都归一，CCTV-14 与 CCTV14 都能命中
        for name in ("CCTV14", "cctv-14", "CCTV 14", "cctv14少儿"):
            self.assertEqual(classify(name), "official", name)


class DeriveTest(unittest.TestCase):
    def test_picks_only_kids_channels(self):
        all_ch = [ch("CCTV-14"), ch("湖南卫视"), ch("海绵宝宝"), ch("金鹰卡通")]
        got = kids_channels(all_ch)
        self.assertEqual([c.name for c in got], ["CCTV-14", "海绵宝宝", "金鹰卡通"])

    def test_cartoons_can_be_excluded(self):
        all_ch = [ch("CCTV-14"), ch("海绵宝宝"), ch("金鹰卡通")]
        got = kids_channels(all_ch, include_cartoons=False)
        self.assertEqual([c.name for c in got], ["CCTV-14", "金鹰卡通"])

    def test_returns_original_objects_group_untouched(self):
        # 派生必须返回原对象、group 保持真实值，否则 /live/c:... 会失效
        original = ch("CCTV-14", group="央视频")
        got = kids_channels([original])
        self.assertIs(got[0], original)
        self.assertEqual(got[0].group, "央视频")

    def test_empty_when_no_kids(self):
        self.assertEqual(kids_channels([ch("湖南卫视"), ch("CCTV-1 综合")]), [])


class RenderTest(unittest.TestCase):
    def test_group_override_only_changes_display(self):
        got = kids_channels([ch("CCTV-14")])
        text, n = render_m3u(got, group_override=KIDS_MODULE, relay_base="http://nas/pw")
        self.assertEqual(n, 1)
        self.assertIn(f'group-title="{KIDS_MODULE}"', text)
        # 播放地址里的频道级 slug 仍按真实 group「第三方聚合源」编码
        self.assertIn("/live/c:", text)
        self.assertNotIn("group-title=\"第三方聚合源\"", text)
        # group 没被改，线路表仍能按它建索引
        self.assertEqual(got[0].group, "第三方聚合源")

    def test_without_override_keeps_real_group(self):
        text, _ = render_m3u([ch("CCTV-14")])
        self.assertIn('group-title="第三方聚合源"', text)


class SettingsTest(unittest.TestCase):
    def test_defaults_on(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings(root=Path("."), data={})
            self.assertTrue(s.kids_enabled)
            self.assertTrue(s.kids_cartoons)

    def test_env_off(self):
        with mock.patch.dict(os.environ, {"HDIPTV_KIDS_ENABLED": "0"}, clear=True):
            s = Settings(root=Path("."), data={})
            self.assertFalse(s.kids_enabled)

    def test_panel_overrides_env(self):
        with mock.patch.dict(os.environ, {"HDIPTV_KIDS_ENABLED": "1"}, clear=True):
            s = Settings(root=Path("."), data={}, panel={"kids_enabled": False, "kids_cartoons": False})
            self.assertFalse(s.kids_enabled)
            self.assertFalse(s.kids_cartoons)

    def test_coerce_panel_bool(self):
        self.assertEqual(
            coerce_panel({"kids_enabled": False, "kids_cartoons": "0"}),
            {"kids_enabled": False, "kids_cartoons": False},
        )

    def test_config_file_fallback(self):
        # Settings.data 就是解析后的 config.toml 各段（不自己读文件）
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings(root=Path("."), data={"kids": {"enabled": False, "cartoons": False}})
            self.assertFalse(s.kids_enabled)
            self.assertFalse(s.kids_cartoons)


def kids_project(disabled: list[str] | None = None, panel_extra: dict | None = None) -> Settings:
    """一份完整项目配置：有 [output]/[kids]，少儿采集源应该被注入。"""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config").mkdir()
    (tmp / "config" / "sources.toml").write_text(
        '[[source]]\nname = "运营商官方源"\ntype = "local"\npath = "hubei.m3u"\nenabled = true\n'
        '[[source]]\nname = "官方直连"\ntype = "subscription"\n'
        'urls = ["http://a/x.m3u"]\nenabled = true\n'
        '[[source]]\nname = "聚合源-多线路"\ntype = "subscription"\n'
        'urls = ["http://b/y.m3u"]\nenabled = true\n',
        encoding="utf-8",
    )
    (tmp / "config" / "config.toml").write_text(
        '[output]\nsecret = ""\n\n[kids]\nenabled = true\ncartoons = true\n',
        encoding="utf-8",
    )
    panel = dict(panel_extra or {})
    if disabled is not None:
        panel["disabled_modules"] = disabled
    if panel:
        (tmp / "config" / "panel.json").write_text(json.dumps(panel), encoding="utf-8")
    return load_settings(tmp)


class IndependentCollectionTest(unittest.TestCase):
    def test_kids_source_survives_other_modules_off(self):
        from iptvhd.pipeline import load_sources

        s = kids_project(disabled=["央视频", "咪咕视频", "第三方聚合源"])
        sources = load_sources(s)
        kids = [x for x in sources if x["name"] == KIDS_MODULE]
        self.assertEqual(len(kids), 1)
        self.assertTrue(kids[0]["enabled"])
        # 官方直连 + 聚合源的镜像都带上了，少儿内容不靠那些模块的开关
        self.assertEqual(len(kids[0]["url_groups"]), 2)
        self.assertFalse(any(x.get("name") == "咪咕视频" for x in sources))

    def test_kids_source_omitted_when_disabled(self):
        from iptvhd.pipeline import load_sources

        s = kids_project(panel_extra={"kids_enabled": False})
        self.assertFalse(any(x["name"] == KIDS_MODULE for x in load_sources(s)))


class KidsCollectorTest(unittest.TestCase):
    def test_collects_three_routes_without_touching_other_modules(self):
        from iptvhd import migu

        tmp = Path(tempfile.mkdtemp())
        rows = [migu.ChannelRow(name="经典动画大集合", pid="123456", group="少儿", logo="")]
        agg = [
            Stream(url="http://cdn/a.m3u8", raw_name="海绵宝宝", group="动画"),
            Stream(url="http://cdn/b.m3u8", raw_name="湖南卫视", group="卫视"),
        ]
        with mock.patch.object(migu, "fetch_channels", return_value=rows) as fetch, \
             mock.patch.object(SubscriptionCollector, "collect", return_value=agg):
            got = KidsCollector(name=KIDS_MODULE, config={
                "data_dir": str(tmp),
                "url_groups": [["http://a/x.m3u"]],
            }).collect(HttpClient())

        names = [s.raw_name for s in got]
        self.assertIn("CCTV-14 少儿", names)       # 央视频：零请求登记
        self.assertIn("经典动画大集合", names)      # 咪咕少儿分类
        self.assertIn("海绵宝宝", names)           # 聚合列表里的动画轮播
        self.assertNotIn("湖南卫视", names)        # 普通台不进来
        self.assertTrue(all(s.group == KIDS_MODULE for s in got))
        self.assertTrue(all(s.source == KIDS_MODULE for s in got))
        kwargs = fetch.call_args.kwargs
        self.assertEqual(kwargs["cates"], (migu._KIDS_CATE_ID,))
        self.assertEqual(kwargs["cache_name"], "migu-kids-channels.json")
        self.assertTrue(kwargs["merge_names"])


class DedupeTest(unittest.TestCase):
    def test_kids_streams_survive_global_dedupe(self):
        from iptvhd.pipeline import _dedupe

        original = Stream(url="http://cdn/a.m3u8", source="第三方聚合源", raw_name="海绵宝宝")
        kids = Stream(url="http://cdn/a.m3u8", source=KIDS_MODULE, raw_name="海绵宝宝")
        kids_dup = Stream(url="http://cdn/a.m3u8", source=KIDS_MODULE, raw_name="海绵宝宝")
        got = _dedupe([original, kids, kids_dup])
        self.assertEqual(len(got), 2)
        self.assertEqual({s.source for s in got}, {"第三方聚合源", KIDS_MODULE})


class SingleSubscriptionTest(unittest.TestCase):
    def _channels(self) -> list[Channel]:
        return [
            Channel(name="CCTV-14 少儿", group=KIDS_MODULE,
                    streams=[Stream(url="http://a", slug="cctv14")]),
            Channel(name="海绵宝宝", group=KIDS_MODULE,
                    streams=[Stream(url="http://b")]),
            Channel(name="海绵宝宝", group="第三方聚合源",
                    streams=[Stream(url="http://c")]),
            Channel(name="湖南卫视", group="第三方聚合源",
                    streams=[Stream(url="http://d")]),
        ]

    def test_kids_group_is_part_of_main_playlist(self):
        s = Settings(root=Path("."), data={}, panel={"kids_enabled": True})
        got = _kids_visible(_module_visible(self._channels(), s), s)
        self.assertEqual({c.group for c in got}, {KIDS_MODULE, "第三方聚合源"})

    def test_kids_off_removes_whole_group(self):
        s = Settings(root=Path("."), data={}, panel={"kids_enabled": False})
        got = _module_visible(self._channels(), s)
        self.assertTrue(all(c.group != KIDS_MODULE for c in got))

    def test_cartoons_off_only_affects_kids_group(self):
        s = Settings(root=Path("."), data={}, panel={"kids_cartoons": False})
        got = _kids_visible(self._channels(), s)
        self.assertNotIn(("海绵宝宝", KIDS_MODULE), [(c.name, c.group) for c in got])
        self.assertIn(("海绵宝宝", "第三方聚合源"), [(c.name, c.group) for c in got])

    def test_render_uses_kids_group_without_override(self):
        text, n = render_m3u([self._channels()[0]])
        self.assertEqual(n, 1)
        self.assertIn(f'group-title="{KIDS_MODULE}"', text)


if __name__ == "__main__":
    unittest.main()
