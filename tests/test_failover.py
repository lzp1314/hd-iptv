"""单地址多线路：一个台一个地址，线路切换在服务端内部完成。

这是「列表里每个台只写一行」和「一条线路挂了也不会播不了」能同时成立的原因。
测试分四层守住：slug 编解码、什么时候该启用、中转层轮换、以及 403 时的实际换线。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iptvhd import writers
from iptvhd.models import Channel, Stream
from iptvhd.relay import Relay, RelayError, channel_name, channel_slug
from iptvhd.service import State, live_target


def _channel(name: str, n_url: int, n_slug: int = 0, height: int = 1080) -> Channel:
    streams = [Stream(url=f"http://up{i}.example/x.m3u8", height=height) for i in range(n_url)]
    streams += [Stream(url="", slug=f"cctv{i}", height=height) for i in range(n_slug)]
    return Channel(name=name, group="测试", streams=streams)


class TestChannelSlug(unittest.TestCase):
    def test_roundtrip(self):
        slug = channel_slug("CCTV-1 综合")
        self.assertTrue(slug.startswith("c:"))
        self.assertEqual(channel_name(slug), "CCTV-1 综合")

    def test_slug_is_url_safe(self):
        slug = channel_slug("湖北·公共")
        # m3u 里的地址会被播放器按 URL 处理，slug 里不能有中文或斜杠
        self.assertTrue(all(ord(c) < 128 for c in slug))
        self.assertNotIn("/", slug)

    def test_decode_rejects_other_prefixes(self):
        self.assertEqual(channel_name("cctv1"), "")
        self.assertEqual(channel_name("o:abc"), "")


class TestFailoverKey(unittest.TestCase):
    def test_single_line_keeps_original_address(self):
        # 只有一条线路时换地址没有任何收益，只会把清单请求从播放器挪到本机
        self.assertEqual(writers.failover_key(_channel("A", 1)), "")

    def test_two_lines_switch_to_channel_address(self):
        self.assertEqual(writers.failover_key(_channel("A", 2)), channel_slug("A"))

    def test_manual_lines_disables_failover(self):
        # 用户主动要「手动切线路」时不能把多行塌缩成同一个地址
        self.assertEqual(writers.failover_key(_channel("A", 3), lines_per_channel=3), "")

    def test_only_slug_lines_not_enough(self):
        # 全是「只登记了频道、播放时现换票」的，线路表里没有可比的东西
        self.assertEqual(writers.failover_key(_channel("A", 0, n_slug=3)), "")

    def test_mixed_lines_ok(self):
        # 一条真地址 + 一条换票源：真地址那条能跑，就值得挂上去
        self.assertEqual(writers.failover_key(_channel("A", 1, n_slug=1)), channel_slug("A"))


class TestRenderUsesChannelAddress(unittest.TestCase):
    def test_multi_line_channel_writes_one_stable_address(self):
        ch = _channel("CCTV-1", 3)
        text, count = writers.render_m3u([ch], relay_base="http://h")
        self.assertEqual(count, 1)
        self.assertIn(f"/live/{channel_slug('CCTV-1')}.m3u8", text)
        # 地址不随更新变化：这一行写死，播放器收藏的一直有效
        again, _ = writers.render_m3u([ch], relay_base="http://h")
        self.assertEqual(text, again)

    def test_failover_can_be_turned_off(self):
        ch = _channel("CCTV-1", 3)
        text, count = writers.render_m3u([ch], relay_base="http://h", failover=False)
        self.assertEqual(count, 1)
        self.assertIn("http://up0.example/x.m3u8", text)

    def test_txt_uses_same_address(self):
        ch = _channel("CCTV-1", 2)
        text, _ = writers.render_txt([ch], relay_base="http://h")
        self.assertIn(f"CCTV-1,http://h/live/{channel_slug('CCTV-1')}.m3u8", text)


class TestLiveTarget(unittest.TestCase):
    def test_channel_slug_resolves_to_name(self):
        hit = live_target(channel_slug("CCTV-1"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "CCTV-1")

    def test_percent_encoded_slug(self):
        # 播放器把 c: 编码成 c%3A 是常见行为
        self.assertIsNotNone(live_target(channel_slug("A").replace(":", "%3A")))

    def test_unknown_still_404(self):
        self.assertIsNone(live_target("nope"))


def _resolver_for(urls: list[str]):
    """一个会「换下一路」的假解析器，签名与 State._resolve 一致。"""
    at = {"i": 0}

    def resolve(slug: str, rotate: bool = False) -> tuple[str, str]:
        if rotate:
            at["i"] = (at["i"] + 1) % len(urls)
        return urls[at["i"]], "ua"

    return resolve


MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:{seq}
#EXTINF:10.000,
seg-{seq}.ts
"""


class TestRelayRotate(unittest.TestCase):
    def test_fresh_alone_does_not_rotate(self):
        """换票（fresh）不等于换线路：地址 TTL 到期也会 fresh，那不是故障。"""
        res = _resolver_for(["http://a/1.m3u8", "http://b/2.m3u8"])
        relay = Relay(res, warm=False)
        self.assertEqual(relay.upstream("c:x")[0], "http://a/1.m3u8")
        relay.forget("c:x")
        self.assertEqual(relay.upstream("c:x", fresh=True)[0], "http://a/1.m3u8")

    def test_rotate_switches_line(self):
        res = _resolver_for(["http://a/1.m3u8", "http://b/2.m3u8"])
        relay = Relay(res, warm=False)
        self.assertEqual(relay.upstream("c:x")[0], "http://a/1.m3u8")
        self.assertEqual(relay.upstream("c:x", rotate=True)[0], "http://b/2.m3u8")
        # 转回第一条：所有线路都试过了，从最该用的那条重新开始
        self.assertEqual(relay.upstream("c:x", rotate=True)[0], "http://a/1.m3u8")

    def test_old_resolver_without_rotate_still_works(self):
        """老解析器只有一个参数，不能因为新功能就把它弄挂。"""
        relay = Relay(lambda slug: ("http://a/1.m3u8", "ua"), warm=False)
        self.assertEqual(relay.upstream("cctv1", rotate=True)[0], "http://a/1.m3u8")

    def test_403_falls_over_within_one_request(self):
        """一次播放器请求内就要换线，不能等它自己重试。

        电视盒子遇错常常直接弹「无法播放」，不会替我们再点一次。
        """
        res = _resolver_for(["http://a/1.m3u8", "http://b/2.m3u8"])
        relay = Relay(res, warm=False)
        calls = []

        def fake_fetch(url, ua, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                return 403, {}, b"", url
            return 200, {}, MEDIA.format(seq=1).encode(), url

        with patch.object(relay, "_fetch", side_effect=fake_fetch), \
                patch("iptvhd.relay.time.sleep"):
            text, base = relay._fetch_raw("c:x")

        self.assertIn("#EXTM3U", text)
        self.assertEqual(calls, ["http://a/1.m3u8", "http://b/2.m3u8"])
        self.assertEqual(base, "http://b/2.m3u8")

    def test_frozen_playlist_rotates(self):
        """清单冻住十有八九是这条线路自己的问题，换票和换线路成本一样。"""
        res = _resolver_for(["http://a/1.m3u8", "http://b/2.m3u8"])
        relay = Relay(res, warm=False)
        relay._absorb("c:x", MEDIA.format(seq=1), "http://a/1.m3u8")
        with patch("iptvhd.relay._FROZEN_AFTER", 0.0), patch("iptvhd.relay.time.sleep"):
            calls = []

            def fake_fetch(url, ua, timeout=None):
                calls.append(url)
                return 200, {}, MEDIA.format(seq=99).encode(), url

            with patch.object(relay, "_fetch", side_effect=fake_fetch):
                relay._prefetch("c:x")
        self.assertIn("http://b/2.m3u8", calls)


class TestStateLines(unittest.TestCase):
    def _state(self, channels: list[dict]) -> State:
        root = Path(tempfile.mkdtemp())
        settings_data = {"app": {"data_dir": "output"}}
        from iptvhd.settings import Settings

        settings = Settings(root=root, data=settings_data)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "latest.json").write_text(
            json.dumps({"channels": channels}, ensure_ascii=False), encoding="utf-8"
        )
        return State(settings)

    def test_resolve_returns_first_line(self):
        state = self._state([
            {"name": "CCTV-1", "streams": [
                {"url": "http://a/1.m3u8", "slug": ""},
                {"url": "http://b/2.m3u8", "slug": ""},
            ]},
        ])
        url, _ = state._resolve(channel_slug("CCTV-1"))
        self.assertEqual(url, "http://a/1.m3u8")

    def test_rotate_moves_to_next_line_and_wraps(self):
        state = self._state([
            {"name": "CCTV-1", "streams": [
                {"url": "http://a/1.m3u8"},
                {"url": "http://b/2.m3u8"},
            ]},
        ])
        slug = channel_slug("CCTV-1")
        self.assertEqual(state._resolve(slug)[0], "http://a/1.m3u8")
        self.assertEqual(state._resolve(slug, rotate=True)[0], "http://b/2.m3u8")
        self.assertEqual(state._resolve(slug, rotate=True)[0], "http://a/1.m3u8")

    def test_official_line_uses_carrier_ua(self):
        # 运营商的 IPv6 边缘只认机顶盒 UA，用浏览器 UA 会 403
        state = self._state([
            {"name": "CCTV-1", "streams": [{"url": "http://a/1.m3u8", "slug": "o:xyz"}]},
        ])
        _, ua = state._resolve(channel_slug("CCTV-1"))
        self.assertIn("okhttp", ua)

    def test_aggregate_line_uses_browser_ua(self):
        state = self._state([
            {"name": "CCTV-1", "streams": [{"url": "http://a/1.m3u8", "slug": "o:xyz"},
                                           {"url": "http://b/2.m3u8", "slug": ""}]},
        ])
        url, ua = state._resolve(channel_slug("CCTV-1"), rotate=True)
        self.assertEqual(url, "http://b/2.m3u8")
        self.assertIn("Mozilla", ua)

    def test_unknown_channel_raises(self):
        state = self._state([{"name": "CCTV-1", "streams": [{"url": "http://a/1.m3u8"}]}])
        with self.assertRaises(RelayError):
            state._resolve(channel_slug("没有这个台"))

    def test_lines_rebuilt_when_result_changes(self):
        """更新一轮之后线路全变了，不能还拿上一轮的结论播。"""
        import time as _t

        state = self._state([{"name": "CCTV-1", "streams": [{"url": "http://a/1.m3u8"},
                                                            {"url": "http://b/2.m3u8"}]}])
        slug = channel_slug("CCTV-1")
        self.assertEqual(state._resolve(slug)[0], "http://a/1.m3u8")

        _t.sleep(0.01)
        (state.settings.data_dir / "latest.json").write_text(
            json.dumps({"channels": [{"name": "CCTV-1", "streams": [
                {"url": "http://c/3.m3u8"}, {"url": "http://d/4.m3u8"}]}]}),
            encoding="utf-8",
        )
        self.assertEqual(state._resolve(slug)[0], "http://c/3.m3u8")

    def test_ysp_slug_still_resolved_by_old_path(self):
        state = self._state([])
        with patch("iptvhd.service._ysp_resolver", return_value=("http://ysp/x.m3u8", "yspua")):
            url, ua = state._resolve("cctv1")
        self.assertEqual(url, "http://ysp/x.m3u8")
        self.assertEqual(ua, "yspua")


if __name__ == "__main__":
    unittest.main()
