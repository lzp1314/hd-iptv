"""B + C：公开转播源增量升级、按省自动取源、面板开关。全程不联网。

重点守住几条边界：自动源只在采集流程里联网、组播/内网地址一律丢、
湖北移动不重复拉、面板开关优先级正确、增量升级不碰用户自己的配置。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from iptvhd import autoregion, pipeline
from iptvhd.service import State
from iptvhd.settings import (
    Settings,
    append_public_sources,
    as_bool,
    coerce_panel,
    load_settings,
)

AH = "http://120.210.193.151:8006/120000001003/live/program/x/index.m3u8"

LIST = f"""央视,#genre#
CCTV-1 综合,{AH}
CCTV-2 财经,http://192.168.123.1:23234/rtp/239.3.1.1:5000
CCTV-3 综艺,http://10.0.0.8:8080/live/3.m3u8
安徽卫视,rtp://239.1.1.1:5000
CCTV-4K,http://[2409:8087:4c00::9]:6610/live/4k.m3u8
CCTV-5 体育,http://[fe80::1]:6610/live/5.m3u8
"""


def _root(tmp: str, province: str = "anhui", isp: str = "mobile") -> tuple[Path, Settings]:
    root = Path(tmp)
    (root / "config").mkdir(exist_ok=True)
    (root / "output").mkdir(exist_ok=True)
    (root / "config" / "sources.toml").write_text(
        '[[source]]\nname = "运营商官方源"\ntype = "local"\npath = "x.m3u"\nenabled = true\n',
        encoding="utf-8",
    )
    (root / "output" / "region.json").write_text(
        json.dumps({"province": province, "isp": isp, "at": time.time()}), encoding="utf-8"
    )
    settings = Settings(root=root, data={})
    settings.sources_file = root / "config" / "sources.toml"
    # 临时目录里不写 channels.txt，让 _template_match 明确走「不过滤」分支，
    # 免得测试读到这里仓库自己的模板、结果随仓库内容变化
    settings.channels_file = root / "config" / "channels.txt"
    settings.panel = {}
    settings.panel_path = root / "config" / "panel.json"
    return root, settings


class TestCleanList(unittest.TestCase):
    def test_keeps_only_public_unicast(self):
        rows = autoregion.clean_list(LIST)
        urls = [u for _, u in rows]
        self.assertIn(AH, urls)
        self.assertIn("http://[2409:8087:4c00::9]:6610/live/4k.m3u8", urls)
        self.assertEqual(len(urls), 2, "组播、内网、IPv6 本地地址都要丢掉")
        self.assertEqual(rows[0][0], "CCTV-1 综合")

    def test_m3u_and_duplicates(self):
        text = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="CCTV1" group-title="央视",CCTV-1 综合\n'
            f"{AH}\n"
            f'#EXTINF:-1,CCTV-1 综合\n{AH}\n'
        )
        rows = autoregion.clean_list(text)
        self.assertEqual(len(rows), 1, "同一地址去重")
        self.assertEqual(rows[0][0], "CCTV-1 综合")

    def test_limit_and_name_sanitize(self):
        lines = [f"CCTV{i},http://1.2.3.{i}/a.m3u8" for i in range(1, 100)]
        rows = autoregion.clean_list("\n".join(lines), limit=5)
        self.assertEqual(len(rows), 5)
        # m3u 的显示名带逗号时，写回 txt 会破坏「名字,地址」格式，得先清掉
        shown = f"#EXTM3U\n#EXTINF:-1,CCTV-1,综合\n{AH}\n"
        self.assertEqual(autoregion.clean_list(shown)[0][0], "CCTV-1 综合")

    def test_empty_placeholder_list(self):
        self.assertEqual(autoregion.clean_list("央视,#genre#\nCCTV-1 综合\n"), [])

    def test_match_filters_out_local_channels(self):
        text = f"央视,#genre#\nCCTV1,{AH}\n安徽公共,http://1.2.3.4/local.m3u8\n"
        rows = autoregion.clean_list(text, match=lambda n: n.startswith("CCTV"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "CCTV1")

    def test_template_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, s = _root(tmp)
            (root / "config" / "channels.txt").write_text(
                "央视,#genre#\nCCTV-1 综合\n卫视,#genre#\n湖南卫视\n", encoding="utf-8"
            )
            match = autoregion._template_match(s)
            self.assertIsNotNone(match)
            self.assertTrue(match("CCTV-1 综合"))
            self.assertTrue(match("湖南卫视"))
            self.assertFalse(match("安徽公共频道"))

    def test_missing_template_does_not_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            self.assertIsNone(autoregion._template_match(s))
            self.assertEqual(len(autoregion.clean_list(LIST, match=None)), 2)


class TestRegionOf(unittest.TestCase):
    def test_manual_wins_over_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, s = _root(tmp, "anhui", "mobile")
            s.data = {"app": {}, "update": {}}
            with mock.patch.dict(os.environ, {"HDIPTV_REGION": "jiangsu", "HDIPTV_ISP": "telecom"}, clear=True):
                self.assertEqual(autoregion.region_of(s), ("jiangsu", "telecom"))

    def test_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp, "anhui", "mobile")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(autoregion.region_of(s), ("anhui", "mobile"))

    def test_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "output").mkdir()
            s = Settings(root=root, data={})
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(autoregion.region_of(s), ("", ""))


class TestSkipReasons(unittest.TestCase):
    def test_hubei_mobile_and_broadnet(self):
        self.assertIn("内置", autoregion._skip_reason("hubei", "mobile"))
        self.assertIn("广电", autoregion._skip_reason("anhui", "broadnet"))
        self.assertEqual(autoregion._skip_reason("anhui", "mobile"), "")

    def test_switch_off_never_touches_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            s.panel["auto_region"] = False
            with mock.patch.object(autoregion, "fetch_list") as fetch:
                self.assertIsNone(autoregion.auto_source(s))
                self.assertIsNone(autoregion.refresh(s))
                fetch.assert_not_called()

    def test_auto_source_is_local_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, s = _root(tmp)
            path = autoregion.cache_path(s, "anhui", "mobile")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"CCTV-1,{AH}\n", encoding="utf-8")
            with mock.patch.object(autoregion, "fetch_list") as fetch, \
                 mock.patch.dict(os.environ, {}, clear=True):
                src = autoregion.auto_source(s)
                fetch.assert_not_called()
            self.assertEqual(src["name"], autoregion.AUTO_SOURCE_NAME)
            self.assertEqual(src["type"], "local")
            self.assertTrue(src["trust"], "本省 CDN 必须串行慢探")
            self.assertTrue(Path(src["path"]).is_absolute())


class TestRefresh(unittest.TestCase):
    def test_fetches_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            logs: list[str] = []
            with mock.patch.object(autoregion, "fetch_list", return_value=(LIST, "ok")), \
                 mock.patch.dict(os.environ, {}, clear=True):
                path = autoregion.refresh(s, logs.append)
            self.assertIsNotNone(path)
            self.assertIn("CCTV-1 综合", path.read_text(encoding="utf-8"))
            self.assertNotIn("192.168", path.read_text(encoding="utf-8"))
            self.assertTrue(any("取到" in x for x in logs))

    def test_fresh_cache_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            with mock.patch.object(autoregion, "fetch_list", return_value=(LIST, "ok")):
                first = autoregion.refresh(s)
            self.assertIsNotNone(first)
            with mock.patch.object(autoregion, "fetch_list") as fetch:
                self.assertEqual(autoregion.refresh(s), first)
                fetch.assert_not_called()

    def test_upstream_down_keeps_old_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            with mock.patch.object(autoregion, "fetch_list", return_value=(LIST, "ok")):
                path = autoregion.refresh(s)
            old = time.time() - 3 * 86400
            os.utime(path, (old, old))
            with mock.patch.object(autoregion, "fetch_list", return_value=("", "fail")):
                self.assertEqual(autoregion.refresh(s), path)

    def test_no_list_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            with mock.patch.object(autoregion, "fetch_list", return_value=("", "missing")):
                self.assertIsNone(autoregion.refresh(s))

    def test_placeholder_list_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            with mock.patch.object(autoregion, "fetch_list",
                                   return_value=("央视,#genre#\nCCTV-1 综合\n", "ok")):
                self.assertIsNone(autoregion.refresh(s))

    def test_hubei_mobile_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp, "hubei", "mobile")
            with mock.patch.object(autoregion, "fetch_list") as fetch:
                self.assertIsNone(autoregion.refresh(s))
                fetch.assert_not_called()


class TestLoadSourcesInjection(unittest.TestCase):
    def test_injects_and_can_be_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, s = _root(tmp)
            path = autoregion.cache_path(s, "anhui", "mobile")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"CCTV-1,{AH}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                names = [x["name"] for x in pipeline.load_sources(s)]
                self.assertIn(autoregion.AUTO_SOURCE_NAME, names)
            with mock.patch.dict(os.environ, {"HDIPTV_SKIP_SOURCES": "本省自动源"}, clear=True):
                names = [x["name"] for x in pipeline.load_sources(s)]
                self.assertNotIn(autoregion.AUTO_SOURCE_NAME, names)

    def test_no_cache_file_no_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, s = _root(tmp)
            with mock.patch.dict(os.environ, {}, clear=True):
                names = [x["name"] for x in pipeline.load_sources(s)]
                self.assertNotIn(autoregion.AUTO_SOURCE_NAME, names)


class TestSwitch(unittest.TestCase):
    def test_as_bool(self):
        for value in (False, "0", "off", "关闭", 0, ""):
            self.assertFalse(as_bool(value), repr(value))
        for value in (True, "1", "on", 1, None, "yes"):
            self.assertTrue(as_bool(value), repr(value))

    def test_priority_panel_env_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.toml").write_text("[update]\nauto_region = false\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HDIPTV_AUTO_REGION": "1"}, clear=True):
                s = load_settings(root)
                self.assertTrue(s.auto_region, "env 盖过 config")
                s.panel["auto_region"] = False
                self.assertFalse(s.auto_region, "面板盖过 env")

    def test_config_default_and_coerce(self):
        self.assertEqual(coerce_panel({"auto_region": "0"})["auto_region"], False)
        self.assertEqual(coerce_panel({"auto_region": True})["auto_region"], True)
        self.assertTrue(coerce_panel({}).get("auto_region") is None)

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "output").mkdir()
            with mock.patch.dict(os.environ, {}, clear=True):
                state = State(load_settings(root))
                self.assertTrue(state.current_settings()["auto_region"])
                out = state.apply_settings({"auto_region": False})
                self.assertFalse(out["auto_region"])
                self.assertFalse(load_settings(root).auto_region, "落盘后仍为关")


class TestPublicSourcesUpgrade(unittest.TestCase):
    def test_default_template_gets_two_sources_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sources.toml"
            p.write_text('[[source]]\nname = "运营商官方源"\ntype = "local"\npath = "x"\n', encoding="utf-8")
            self.assertTrue(append_public_sources(p))
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            names = [x["name"] for x in data["source"]]
            self.assertIn("聚合源-公开转播", names)
            self.assertIn("聚合源-精简线路", names)
            before = p.read_bytes()
            self.assertFalse(append_public_sources(p), "有标记就不再追加")
            self.assertEqual(p.read_bytes(), before)

    def test_user_config_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sources.toml"
            body = '[[source]]\nname = "我自己写的"\ntype = "subscription"\nurl = "http://x/1.m3u"\n'
            p.write_text(body, encoding="utf-8")
            self.assertFalse(append_public_sources(p))
            self.assertEqual(p.read_text(encoding="utf-8"), body)

    def test_same_name_source_only_adds_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sources.toml"
            p.write_text(
                '[[source]]\nname = "运营商官方源"\ntype = "local"\npath = "x"\n'
                '[[source]]\nname = "聚合源-公开转播"\ntype = "subscription"\nurl = "http://y/1.m3u"\n',
                encoding="utf-8",
            )
            self.assertFalse(append_public_sources(p))
            text = p.read_text(encoding="utf-8")
            self.assertEqual(text.count('name = "聚合源-公开转播"'), 1)
            self.assertIn("public-sources-v1", text)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(append_public_sources(Path(tmp) / "nope.toml"))


class TestHubeiComposeIsolation(unittest.TestCase):
    """湖北版只跳两条全国聚合源，官方源必须留着，源名改了这里要红。"""

    ROOT = Path(__file__).resolve().parents[1]

    def _skip_value(self) -> set[str]:
        text = (self.ROOT / "docker-compose.hubei.yml").read_text(encoding="utf-8")
        m = re.search(r'^\s*HDIPTV_SKIP_SOURCES:\s*"([^"]*)"', text, re.M)
        self.assertIsNotNone(m, "湖北版必须显式写死 HDIPTV_SKIP_SOURCES")
        return {x.strip() for x in m.group(1).replace("，", ",").split(",") if x.strip()}

    def test_names_match_default_template(self):
        skipped = self._skip_value()
        self.assertEqual(skipped, {"聚合源-公开转播", "聚合源-精简线路"})
        self.assertNotIn("运营商官方源", skipped, "湖北版不能把官方源一起跳掉")
        for rel in ("config/sources.toml", "iptvhd/defaults/sources.toml"):
            data = tomllib.loads((self.ROOT / rel).read_text(encoding="utf-8"))
            names = {str(x.get("name", "")) for x in data["source"]}
            self.assertTrue(skipped <= names, f"{rel} 里找不到这两个源名，跳过会失效")

    def test_load_sources_keeps_official_source(self):
        settings = load_settings(str(self.ROOT))
        env = {
            "HDIPTV_SKIP_SOURCES": ",".join(sorted(self._skip_value())),
            "HDIPTV_AUTO_REGION": "0",
        }
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(autoregion, "auto_source", return_value=None):
            names = [str(x.get("name", "")) for x in pipeline.load_sources(settings)]
        self.assertIn("运营商官方源", names)
        self.assertNotIn("聚合源-公开转播", names)
        self.assertNotIn("聚合源-精简线路", names)

    def test_generic_compose_still_gets_public_sources(self):
        """通用版不受湖北版影响：跳过官方源，两条聚合源照常加载。"""
        settings = load_settings(str(self.ROOT))
        env = {"HDIPTV_SKIP_SOURCES": "运营商官方源", "HDIPTV_AUTO_REGION": "0"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(autoregion, "auto_source", return_value=None):
            names = [str(x.get("name", "")) for x in pipeline.load_sources(settings)]
        self.assertNotIn("运营商官方源", names)
        self.assertIn("聚合源-公开转播", names)
        self.assertIn("聚合源-精简线路", names)


if __name__ == "__main__":
    unittest.main()
