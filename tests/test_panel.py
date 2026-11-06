"""面板设置、订阅列表渲染、节目单裁剪、台标缓存。不联网。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from iptvhd import epg, logos, writers
from iptvhd.models import Channel, Stream
from iptvhd.service import _channels_from_result, _visible
from iptvhd.settings import clean_names, coerce_panel, load_settings, read_panel, write_panel

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="1"><display-name>CCTV-1 综合</display-name></channel>
  <channel id="2"><display-name>湖南卫视</display-name></channel>
  <channel id="3"><display-name>某地方台</display-name></channel>
  <programme channel="1" start="20261106120000 +0800" stop="20261106130000 +0800">
    <title>新闻30分</title>
  </programme>
  <programme channel="2" start="20261106120000 +0800" stop="20261106130000 +0800">
    <title>午间新闻</title>
  </programme>
  <programme channel="3" start="20261106120000 +0800" stop="20261106130000 +0800">
    <title>不相关</title>
  </programme>
</tv>
"""


def _channel(name: str, group: str, slug: str, height: int = 1080) -> Channel:
    return Channel(
        name=name,
        group=group,
        streams=[Stream(url=f"http://up/{slug}.m3u8", slug=slug, height=height, width=height * 16 // 9)],
    )


class TestPanelSettings(unittest.TestCase):
    def test_roundtrip_and_coerce(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "panel.json"
            self.assertEqual(read_panel(path), {})
            write_panel(path, {"interval_hours": 1.5, "playlist_ttl": 5, "cache_mb": 128})
            data = read_panel(path)
            self.assertEqual(data["interval_hours"], 1.5)
            self.assertEqual(data["cache_mb"], 128)

    def test_bounds_and_types(self):
        clean = coerce_panel({"interval_hours": 999, "playlist_ttl": 0, "cache_mb": -5, "没用": 1})
        self.assertEqual(clean["interval_hours"], 168.0)   # 夹到上限
        self.assertEqual(clean["playlist_ttl"], 0.5)       # 夹到下限
        self.assertEqual(clean["cache_mb"], 0)
        self.assertNotIn("没用", clean)

    def test_panel_beats_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.toml").write_text(
                '[update]\ninterval_hours = 6\n[output]\npublic_url = "http://old:9797"\n',
                encoding="utf-8",
            )
            write_panel(root / "config" / "panel.json", {"interval_hours": 2, "public_url": "https://tv.example.com/"})
            settings = load_settings(root)
            self.assertEqual(settings.interval_hours, 2)
            self.assertEqual(settings.public_url, "https://tv.example.com")

    def test_defaults_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            self.assertEqual(settings.playlist_ttl, 2.5)
            self.assertEqual(settings.relay_cache_mb, 96.0)
            self.assertEqual(settings.public_url, "")


class TestHiddenChannels(unittest.TestCase):
    """隐藏频道：只影响对外输出，原始数据一条不删。"""

    def test_clean_names(self):
        self.assertEqual(clean_names(["CCTV-1", "CCTV-1", " 湖南卫视 "]), ["CCTV-1", "湖南卫视"])
        self.assertEqual(clean_names("CCTV-1"), ["CCTV-1"])
        self.assertEqual(clean_names(None), [])
        self.assertEqual(clean_names([""]), [])
        # 别让一个畸形请求把 panel.json 撑爆
        self.assertEqual(len(clean_names([f"台{i}" for i in range(900)])), 500)

    def test_panel_hidden_beats_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.toml").write_text(
                '[output]\nhidden = ["CCTV-2"]\n', encoding="utf-8"
            )
            settings = load_settings(root)
            self.assertEqual(settings.hidden, ["CCTV-2"])
            # 面板里显式清空 = 全部恢复，不能被配置文件里的旧值顶回来
            write_panel(settings.panel_path, {"hidden": []})
            self.assertEqual(load_settings(root).hidden, [])

    def test_visible_filters_by_name(self):
        channels = [_channel("CCTV-1", "央视", "cctv1"), _channel("CCTV-2", "央视", "cctv2")]
        self.assertEqual([c.name for c in _visible(channels, [])], ["CCTV-1", "CCTV-2"])
        self.assertEqual([c.name for c in _visible(channels, ["CCTV-2"])], ["CCTV-1"])
        # 名字对不上就不过滤——隐藏一个不存在的台不该误伤别的台
        self.assertEqual(len(_visible(channels, ["CCTV-9"])), 2)

    def test_result_keeps_hidden_rows(self):
        """服务端返回全部频道并打标记，面板才能做「恢复」。"""
        result = {"channels": [
            {"name": "CCTV-1", "group": "央视", "streams": [{"url": "", "slug": "cctv1"}]},
            {"name": "CCTV-2", "group": "央视", "streams": [{"url": "", "slug": "cctv2"}]},
        ]}
        channels = _channels_from_result(result)
        self.assertEqual(len(_visible(channels, ["CCTV-2"])), 1)
        self.assertEqual(len(channels), 2)          # 原始结果没被改动


class TestRenderM3u(unittest.TestCase):
    def test_relay_and_attributes(self):
        channels = [_channel("CCTV-1", "央视", "cctv1")]
        text, count = writers.render_m3u(
            channels,
            epg_url="http://h/epg.xml",
            logo_base="http://h/logo",
            relay_base="http://h",
            tvg_ids={"CCTV-1": "1"},
        )
        self.assertEqual(count, 1)
        first = text.splitlines()[0]
        self.assertIn('x-tvg-url="http://h/epg.xml"', first)
        self.assertIn('url-tvg="http://h/epg.xml"', first)
        self.assertIn('tvg-id="1"', text)
        self.assertIn("tvg-logo=", text)
        self.assertIn("/logo/CCTV-1.png", text)
        self.assertIn("http://h/live/cctv1.m3u8", text)

    def test_no_duplicate_when_relay_collapses(self):
        channel = Channel(
            name="CCTV-1",
            group="央视",
            streams=[
                Stream(url="http://up/a.m3u8", slug="cctv1", height=1080),
                Stream(url="http://up/b.m3u8", slug="cctv1", height=1080),
            ],
        )
        text, count = writers.render_m3u([channel], relay_base="http://h")
        self.assertEqual(count, 1)
        self.assertEqual(text.count("#EXTINF"), 1)

    def test_direct_url_without_relay(self):
        text, _ = writers.render_m3u([_channel("CCTV-1", "央视", "cctv1")])
        self.assertIn("http://up/cctv1.m3u8", text)
        self.assertNotIn("/live/", text)


class TestEpgCrop(unittest.TestCase):
    def test_key_of(self):
        self.assertEqual(epg.key_of("CCTV-1"), "cctv1")
        self.assertEqual(epg.key_of("CCTV-1 综合"), "cctv1")
        self.assertEqual(epg.key_of("CCTV-4 (亚洲)"), "cctv4")
        self.assertEqual(epg.key_of("CCTV-5+"), "cctv5+")
        self.assertEqual(epg.key_of("CCTV4K"), "cctv4k")     # 4K 是独立频道
        self.assertEqual(epg.key_of("湖南卫视高清"), "湖南卫视")
        self.assertEqual(epg.key_of("CGTN 法语"), "cgtn法语")  # 语种不能塌缩

    def test_crop_keeps_only_our_channels(self):
        mapping, body, total = epg._crop(XMLTV, ["CCTV-1", "湖南卫视"])
        self.assertEqual(mapping["CCTV-1"], "1")
        self.assertEqual(mapping["湖南卫视"], "2")
        self.assertEqual(total, 2)
        text = body.decode("utf-8")
        self.assertIn("新闻30分", text)
        self.assertNotIn("不相关", text)

    def test_missing_source_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = epg.Epg(Path(tmp), source="")
            self.assertEqual(cache.build(["CCTV-1"]), {})
            self.assertTrue(cache.error)
            self.assertIsNone(cache.xml(["CCTV-1"]))


class TestLogos(unittest.TestCase):
    def test_placeholder_is_valid_png(self):
        self.assertTrue(logos.PLACEHOLDER.startswith(logos.PNG_MAGIC))

    def test_cached_file_served_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = logos.Logos(Path(tmp))
            target = store.path_of("CCTV-1")
            target.write_bytes(logos.PLACEHOLDER)
            self.assertEqual(store.get("CCTV-1"), store.get("CCTV-1"))
            self.assertGreaterEqual(len(store.get("CCTV-1")), len(logos.PLACEHOLDER))

    def test_name_candidates(self):
        self.assertIn("CCTV1", logos.candidates("CCTV-1"))
        self.assertEqual(len(logos.candidates("湖南卫视")), 1)


class TestResultToChannels(unittest.TestCase):
    def test_roundtrip(self):
        from iptvhd.service import _channels_from_result

        payload = json.loads(
            json.dumps(
                {
                    "channels": [
                        {"name": "CCTV-1", "group": "央视", "streams": [{"url": "http://u/a", "slug": "cctv1", "height": 1080}]},
                        {"name": "空台", "group": "其它", "streams": []},
                    ]
                }
            )
        )
        channels = _channels_from_result(payload)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].streams[0].slug, "cctv1")
        self.assertEqual(channels[0].streams[0].grade, "1080P")


if __name__ == "__main__":
    unittest.main()
