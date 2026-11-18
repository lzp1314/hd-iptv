"""咪咕视频：换票协议、账号隔离、频道表与播放接入。

要点：
- 采集只登记 pID、不换票、不带账号；
- 账号与令牌绝不进播放地址、日志、订阅；
- 换票后给播放器的是「未请求过」的调度地址（就近取流的前提）；
- 平台拒绝时降档、账号失效时如实提示，都不中断播放。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptvhd import migu
from iptvhd.net import Response
from iptvhd.settings import Settings, coerce_panel, load_settings

WEB = Path(__file__).resolve().parent.parent / "web"


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0) if self.responses else Response(status=502, body=b"")


def json_resp(data: dict, status: int = 200) -> Response:
    return Response(status=status, body=json.dumps(data).encode())


class TestProtocol(unittest.TestCase):
    def setUp(self):
        migu.reset_state()

    def test_dd_calcu_is_stable_and_shaped(self):
        a = migu._dd_calcu("abcdefghijklmnop", "608807420", 2)
        b = migu._dd_calcu("abcdefghijklmnop", "608807420", 2)
        self.assertEqual(a, b)
        self.assertTrue(a)
        # 首尾交错：第 1、2 个字符来自 puData 的末位和首位
        self.assertEqual(a[0], "p")
        self.assertEqual(a[1], "a")

    def test_seal_skips_when_no_pudata(self):
        raw = "http://cdn.example.com/index.m3u8?token=abc"
        self.assertEqual(migu._seal(raw, "608807420", 2), raw)

    def test_seal_adds_checksum_when_pudata(self):
        raw = "http://cdn.example.com/index.m3u8?a=1&puData=abcdefghijklmnop"
        out = migu._seal(raw, "608807420", 2)
        self.assertIn("&ddCalcu=", out)
        self.assertTrue(out.endswith("&sv=10004&ct=android"))

    def test_clean_account_rejects_header_breakers(self):
        self.assertEqual(migu.clean_account(" abc ", " tok "), ("abc", "tok"))
        self.assertEqual(migu.clean_account("ab\ncd", "tok"), ("", ""))
        self.assertEqual(migu.clean_account("中文", "tok"), ("", ""))
        self.assertFalse(migu.account_ok("a\nb", "tok"))

    def test_ticket_guest_returns_unsigned_dispatch_url(self):
        data = {
            "rid": "SUCCESS",
            "body": {
                "urlInfo": {"url": "http://dispatch.example.com/play?x=1&puData=abcdefghijklmnop", "rateType": 2},
                "auth": {"logined": False},
            },
        }
        c = FakeClient([json_resp(data)])
        t = migu.ticket("608807420", 2, client=c, attempts=1)
        self.assertTrue(t.url.startswith("http://dispatch.example.com/play"))
        self.assertIn("&ddCalcu=", t.url)
        self.assertFalse(t.logined)
        self.assertEqual(t.rate, 2)
        # 关键：只请求了换票接口，没有去请求调度地址
        self.assertEqual(len(c.calls), 1)
        self.assertIn("playurl", c.calls[0][0])

    def test_account_never_in_url_and_guest_rate_skips_account(self):
        data = {
            "rid": "SUCCESS",
            "body": {
                "urlInfo": {"url": "http://dispatch.example.com/play", "rateType": 3},
                "auth": {"logined": True},
            },
        }
        c = FakeClient([json_resp(data)])
        t = migu.ticket("608807420", 3, user_id="u1", token="SECRET123", client=c, attempts=1)
        self.assertNotIn("SECRET123", t.url)
        self.assertNotIn("u1", t.url)
        headers = c.calls[0][1]["headers"]
        self.assertEqual(headers["UserId"], "u1")
        self.assertEqual(headers["UserToken"], "SECRET123")

    def test_guest_rate_ignores_account(self):
        data = {
            "rid": "SUCCESS",
            "body": {
                "urlInfo": {"url": "http://dispatch.example.com/play", "rateType": 2},
                "auth": {"logined": False},
            },
        }
        c = FakeClient([json_resp(data)])
        migu.ticket("608807420", 2, user_id="u1", token="SECRET123", client=c, attempts=1)
        headers = c.calls[0][1]["headers"]
        self.assertNotIn("UserId", headers)
        self.assertNotIn("UserToken", headers)

    def test_member_denied_falls_back_to_720p(self):
        refused = {"rid": "TIPS_NEED_MEMBER", "body": {"urlInfo": {"rateType": 3}}}
        ok = {
            "rid": "SUCCESS",
            "body": {
                "urlInfo": {"url": "http://dispatch.example.com/play", "rateType": 3},
                "auth": {"logined": True},
            },
        }
        c = FakeClient([json_resp(refused), json_resp(ok)])
        t = migu.ticket("608807420", 9, user_id="u1", token="SECRET123", client=c, attempts=2, pause=0)
        self.assertEqual(t.rate, 3)
        self.assertEqual(len(c.calls), 2)
        self.assertIn("rateType=9", c.calls[0][0])
        self.assertIn("rateType=3", c.calls[1][0])

    def test_unavailable_raises_specific(self):
        c = FakeClient([json_resp({"rid": "TIPS_NO_RIGHT", "message": "无权限"})])
        with self.assertRaises(migu.MiguUnavailable):
            migu.ticket("608807420", 3, client=c, attempts=1)

    def test_failed_fetch_is_readable(self):
        c = FakeClient([Response(status=0, body=b"", error="timeout")])
        with self.assertRaises(migu.MiguError) as cm:
            migu.ticket("608807420", 2, client=c, attempts=1, pause=0)
        self.assertIn("无响应", str(cm.exception))


class TestChannels(unittest.TestCase):
    def setUp(self):
        migu.reset_state()

    def test_fetch_and_cache_and_name_of(self):
        cates = {"body": {"liveList": [{"name": "央视"}]}}
        data = {
            "body": {
                "dataList": [
                    {"name": "CCTV1综合", "pID": "608807420", "pics": {}},
                    {"name": "重复台", "pID": "608807420", "pics": {}},
                    {"name": "无ID", "pID": "", "pics": {}},
                ]
            }
        }
        tmp = Path(tempfile.mkdtemp())
        c = FakeClient([json_resp(cates), json_resp(data)])
        rows = migu.fetch_channels(c, data_dir=tmp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "CCTV1综合")
        self.assertEqual(migu.name_of("608807420"), "CCTV1综合")
        # 第二次走缓存，不再发请求
        c2 = FakeClient([])
        again = migu.fetch_channels(c2, data_dir=tmp)
        self.assertEqual(len(again), 1)
        self.assertEqual(len(c2.calls), 0)


class TestSettings(unittest.TestCase):
    def _settings(self, text: str = "") -> Settings:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config").mkdir(parents=True, exist_ok=True)
        (tmp / "config" / "config.toml").write_text(text, encoding="utf-8")
        return load_settings(tmp)

    def test_defaults(self):
        s = self._settings()
        self.assertTrue(s.migu_enabled)
        self.assertEqual(s.migu_rate, 2)
        self.assertTrue(s.migu_h265)
        self.assertTrue(s.migu_client_dispatch)
        self.assertEqual(s.migu_user_id, "")

    def test_panel_over_config(self):
        s = self._settings(
            "[migu]\nenabled = false\nrate = 9\nh265 = false\nclient_dispatch = false\n"
        )
        s.panel.update({
            "migu_enabled": True,
            "migu_rate": 3,
            "migu_h265": True,
            "migu_client_dispatch": True,
        })
        self.assertTrue(s.migu_enabled)
        self.assertEqual(s.migu_rate, 3)
        self.assertTrue(s.migu_h265)
        self.assertTrue(s.migu_client_dispatch)

    def test_env_over_config(self):
        s = self._settings("[migu]\nrate = 2\n")
        with patch.dict("os.environ", {"HDIPTV_MIGU_RATE": "4"}):
            self.assertEqual(s.migu_rate, 4)

    def test_bad_rate_falls_back(self):
        s = self._settings("[migu]\nrate = 99\n")
        self.assertEqual(s.migu_rate, 2)

    def test_coerce_panel_token_empty_kept_out(self):
        out = coerce_panel({"migu_token": "", "migu_user_id": "abc"})
        self.assertNotIn("migu_token", out)
        self.assertEqual(out["migu_user_id"], "abc")
        self.assertIn("migu_token", coerce_panel({"migu_token": "secret"}))

    def test_coerce_panel_rejects_bad_rate(self):
        self.assertNotIn("migu_rate", coerce_panel({"migu_rate": 5}))
        self.assertEqual(coerce_panel({"migu_rate": "7"})["migu_rate"], 7)

    def test_default_template_has_migu_section(self):
        # 全新部署从模板复制配置；模板没有这一段的话咪咕源不会被注入
        from iptvhd.settings import DEFAULTS_DIR

        text = (DEFAULTS_DIR / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[migu]", text)
        self.assertIn("client_dispatch = true", text)

    def test_upgrade_adds_migu_to_full_config(self):
        from iptvhd.settings import upgrade_config

        tmp = Path(tempfile.mkdtemp())
        path = tmp / "config.toml"
        path.write_text("[relay]\nmode = \"direct\"\n[output]\nfailover = true\n", encoding="utf-8")
        self.assertTrue(upgrade_config(path))
        self.assertIn("[migu]", path.read_text(encoding="utf-8"))
        # 最小配置（没有 [output]）不该被补段
        path2 = tmp / "minimal.toml"
        path2.write_text("[probe]\nconcurrency = 2\n", encoding="utf-8")
        self.assertFalse(upgrade_config(path2))


class TestStandalone(unittest.TestCase):
    """咪咕独立分组：全量输出，但不打扰模板频道的补线路。"""

    def test_streams_group_by_source_module(self):
        from iptvhd.models import Stream
        from iptvhd.pipeline import _module_channels

        def st(url, slug, name, source, **kw):
            return Stream(url=url, slug=slug, raw_name=name, source=source,
                          channel=name, ok=True, **kw)

        rows = _module_channels([
            st("", "m:608807420", "CCTV-1", "咪咕视频", trusted=True, playlist_only=True),
            st("", "m:641886683", "湖南卫视", "咪咕视频", trusted=True, playlist_only=True),
            st("http://cdn.x.com/a.m3u8", "", "CCTV-1", "运营商官方源"),
            st("http://cdn.x.com/b.m3u8", "", "CCTV-1", "运营商官方源"),
            st("http://cdn.y.com/c.m3u8", "", "浙江卫视", "聚合源-多线路"),
        ], 2, lambda _: None)
        self.assertEqual(
            {(c.group, c.name) for c in rows},
        {("咪咕视频", "CCTV-1"), ("咪咕视频", "湖南卫视"),
         ("运营商官方源", "CCTV-1"), ("第三方聚合源", "浙江卫视")},
        )
        local = [c for c in rows if c.group == "运营商官方源"][0]
        self.assertEqual(len(local.streams), 2)  # 同模块同台聚合成两条线路

    def test_collector_keeps_official_flag_after_tagging(self):
        from iptvhd.generators import MiguCollector

        migu.reset_state()
        cates = {"body": {"liveList": [{"name": "央视"}]}}
        data = {"body": {"dataList": [{"name": "CCTV1综合", "pID": "608807420", "pics": {}}]}}
        tmp = Path(tempfile.mkdtemp())
        client = FakeClient([json_resp(cates), json_resp(data)])
        got = MiguCollector(config={"data_dir": str(tmp)}).collect(client)
        self.assertEqual(len(got), 1)
        # _tag() 会用 config["trust"] 覆盖，这里必须是补回来的 True
        self.assertTrue(got[0].trusted)
        self.assertEqual(got[0].source, "migu")

    def test_filter_exempts_official_ticketed_540p(self):
        from iptvhd.models import Stream
        from iptvhd.pipeline import _filter

        tmp = Path(tempfile.mkdtemp())
        settings = load_settings(tmp)  # 默认 min_height=720
        official = Stream(url="", slug="m:608807420", raw_name="CCTV1综合",
                          trusted=True, height=540, playlist_only=True, ok=True)
        aggregate = Stream(url="http://cdn.x.com/live.m3u8", raw_name="CCTV1综合",
                           height=540, ok=True)
        self.assertEqual(_filter([official, aggregate], settings), [official])

    def test_render_writes_standalone_group(self):
        from iptvhd import writers
        from iptvhd.models import Channel, Stream

        ch = Channel(name="咪咕独有台", group="咪咕视频", streams=[
            Stream(url="", slug="m:123456", raw_name="咪咕独有台",
                   trusted=True, playlist_only=True, ok=True),
        ])
        # 只有一个源、一条线路时不需要频道级地址，直接给 m: 现换票地址
        text, count = writers.render_m3u([ch], relay_base="http://nas.local")
        self.assertEqual(count, 1)
        self.assertIn('group-title="咪咕视频"', text)
        self.assertIn("/live/m:123456.m3u8", text)

        # 同模块同台有多条线路时走频道级地址，且模块名进 slug，不会和别的模块串线
        ch.streams.append(Stream(url="http://cdn.x.com/backup.m3u8", raw_name="咪咕独有台", ok=True))
        text, _ = writers.render_m3u([ch], relay_base="http://nas.local")
        from iptvhd.relay import channel_slug
        wanted = channel_slug("咪咕独有台", "咪咕视频")
        self.assertIn(f"/live/{wanted}.m3u8", text)
        self.assertNotEqual(wanted, channel_slug("咪咕独有台", "运营商官方源"))


class TestServiceAndPanel(unittest.TestCase):
    def test_migu_pid_parsing(self):
        from iptvhd.service import migu_pid

        self.assertEqual(migu_pid("m:608807420"), "608807420")
        self.assertEqual(migu_pid("m:abc"), "")
        self.assertEqual(migu_pid("cctv1"), "")

    def test_live_target_recognizes_migu(self):
        from iptvhd.service import live_target

        migu._NAMES["608807420"] = "CCTV1综合"
        hit = live_target("m:608807420")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "CCTV1综合")

    def test_panel_has_migu_view_and_controls(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        # 咪咕不再单独占一个导航页，配置块嵌在「数据源」页里，点咪咕模块展开
        self.assertNotIn('data-view="migu"', html)
        self.assertIn('id="builtinModules"', html)
        self.assertIn('id="miguConfig"', html)
        # 配置块必须位于 modulePanel 内部，否则切到数据源页也看不到
        start = html.index('id="modulePanel"')
        end = html.index('id="regionPanel"')
        block = html[start:end]
        for eid in ("setMiguEnabled", "setMiguUser", "setMiguToken", "setMiguRate",
                    "setMiguH265", "setMiguDispatch", "btnMiguSave"):
            self.assertIn(f'id="{eid}"', block)

    def test_view_whitelist_has_every_nav_item(self):
        # 回归点咪咕/数据源却跳回状态页的 bug：白名单必须覆盖所有导航项
        import re
        js = (WEB / "app.js").read_text(encoding="utf-8")
        html = (WEB / "index.html").read_text(encoding="utf-8")
        nav = html[html.index("<nav"):html.index("</nav>")]
        navs = set(re.findall(r'data-view="(\w+)"', nav))
        m = re.search(r'const VIEWS = \[([^\]]+)\]', js)
        self.assertIsNotNone(m, "找不到视图白名单常量")
        views = set(re.findall(r'"(\w+)"', m.group(1)))
        self.assertEqual(navs - views, set(), "有导航项没进白名单，点了会退回状态页")

    def test_app_only_submits_migu_fields_on_migu_save(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('btnId === "btnMiguSave"', js)
        # 令牌只在非空时提交
        self.assertIn("if (miguToken) payload.migu_token = miguToken", js)

    def test_migu_beats_aggregate_but_not_local(self):
        from iptvhd.models import Stream

        migu_s = Stream(url="", slug="m:608807420", trusted=True,
                        height=540, playlist_only=True, ok=True)
        agg = Stream(url="http://cdn.x.com/live.m3u8?time=123",
                     height=1080, speed_mbps=5, ok=True)
        local = Stream(url="http://[2409:abcd]/iptv.m3u8", trusted=True,
                       height=1080, speed_mbps=8, ok=True)
        # 官方现换票源赢第三方聚合（外省流畅）
        self.assertLess(migu_s.sort_key(), agg.sort_key())
        # 本省运营商源靠真实画质/测速赢咪咕（本省不受影响）
        self.assertLess(local.sort_key(), migu_s.sort_key())


if __name__ == "__main__":
    unittest.main()
