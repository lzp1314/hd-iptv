"""自定义源：逐条判定「直连 / 经本机转发」，以及接进主流程的几处豁免。

这里守的是四条容易回退的行为：

1. 判定按**每条流**做，不按整个源（一个 /playlist.m3u 里可能整份都是裸 TS）；
2. 裸流不被包成 /live/xxx.m3u8（转发层只认 m3u8，包了就是 100% 打不开）；
3. 自定义源不套画质门槛（本地源报不出分辨率，套了整组台会被筛光）；
4. 不参与「一址多台」剔除（rtp2http 可能把组播参数放在 query 里）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from iptvhd import custom
from iptvhd.collectors import CustomCollector
from iptvhd.models import Stream
from iptvhd.net import Response
from iptvhd.pipeline import _filter, _route_through_relay


class FakeClient:
    """只回一份固定内容，够测采集与识别。"""

    def __init__(self, body: bytes = b"", url: str = "", status: int = 200) -> None:
        self.body = body
        self.url = url
        self.status = status
        self.asked: list[str] = []

    def get(self, url: str, **kwargs) -> Response:
        self.asked.append(url)
        return Response(
            url=self.url or url, status=self.status, headers={}, body=self.body,
            elapsed_ms=200,
        )


def _settings(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(sources_file=tmp / "config" / "sources.toml")


M3U_MIXED = """#EXTM3U
#EXTINF:-1 tvg-name="CCTV1" group-title="x",CCTV1
http://nas:7080/udp/239.1.1.1:1234
#EXTINF:-1,CCTV2
http://cdn.example.com/cctv2.m3u8
"""


class ModeRuleTest(unittest.TestCase):
    def test_auto_uses_suffix(self):
        self.assertTrue(custom.wants_relay("http://a/b/cctv.m3u8"))
        self.assertTrue(custom.wants_relay("http://a/b/list.m3u?x=1"))
        self.assertFalse(custom.wants_relay("http://nas:7080/udp/239.1.1.1:1234"))

    def test_force_direct_wins_over_suffix(self):
        # 后缀谎报（裸流写成 .m3u8）时，用户能自己兜底
        self.assertFalse(custom.wants_relay("http://a/b/cctv.m3u8", "direct"))

    def test_force_relay_for_raw(self):
        self.assertTrue(custom.wants_relay("http://nas:7080/udp/239.1.1.1:1234", "relay"))

    def test_bad_mode_falls_back_to_auto(self):
        self.assertEqual(custom.coerce_mode("DIRECT "), "direct")
        self.assertEqual(custom.coerce_mode("乱填"), "auto")


class StoreTest(unittest.TestCase):
    def test_roundtrip(self):
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            items = [
                custom.CustomSource(name="本地组播", url="http://nas:7080/playlist.m3u",
                                    mode="auto", ua="okhttp/3.12.0"),
                custom.CustomSource(name="自家源", lines="CCTV1,http://nas:7080/udp/1.1.1.1:1",
                                    enabled=False),
            ]
            custom.save(settings, items)
            loaded = custom.load(settings)
            self.assertEqual([x.name for x in loaded], ["本地组播", "自家源"])
            self.assertEqual(loaded[0].ua, "okhttp/3.12.0")
            self.assertFalse(loaded[1].enabled)
            self.assertTrue(loaded[1].id)

    def test_broken_file_is_empty_not_fatal(self):
        with TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            path = custom.store_path(settings)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ 这不是 json", encoding="utf-8")
            self.assertEqual(custom.load(settings), [])

    def test_payload_filters_and_dedupes(self):
        values = {"sources": [
            {"name": "A", "url": "http://a/1.m3u8"},
            {"name": "A", "url": "http://a/2.m3u8"},          # 同名并组，留第一条
            {"name": "", "url": "http://a/3.m3u8"},           # 缺名字
            {"name": "B", "url": "ftp://a/4"},                # 协议不支持
            {"name": "C", "lines": "台1,http://a/udp/1.1.1.1:1", "mode": "乱写"},
        ]}
        items = custom.parse_payload(values)
        self.assertEqual([x.name for x in items], ["A", "C"])
        self.assertEqual(items[0].url, "http://a/1.m3u8")
        self.assertEqual(items[1].mode, "auto")

    def test_payload_requires_list(self):
        with self.assertRaises(ValueError):
            custom.parse_payload({"sources": "不是数组"})


class CollectTest(unittest.TestCase):
    def _collect(self, body: bytes, **config) -> list[Stream]:
        config.setdefault("url", "http://nas:7080/playlist.m3u")
        cfg = {"name": "本地组播", "mode": "auto", **config}
        return CustomCollector(name="本地组播", config=cfg).collect(
            FakeClient(body=body, url=cfg["url"])
        )

    def test_playlist_judged_per_stream(self):
        streams = self._collect(M3U_MIXED.encode())
        self.assertEqual(len(streams), 2)
        self.assertTrue(streams[0].direct, "裸 TS 必须直连")
        self.assertFalse(streams[1].direct, "m3u8 走本机转发")
        self.assertTrue(all(s.custom for s in streams))

    def test_raw_ts_becomes_single_channel(self):
        body = b"\x47" + b"\x00" * 4000
        streams = self._collect(body, url="http://nas:7080/udp/239.1.1.1:1234")
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].direct)
        self.assertEqual(streams[0].raw_name, "本地组播")

    def test_txt_lines(self):
        body = "CCTV1,http://nas:7080/udp/239.1.1.1:1234\nCCTV2,http://nas:7080/udp/239.1.1.2:1234\n"
        streams = self._collect(body.encode())
        self.assertEqual([s.raw_name for s in streams], ["CCTV1", "CCTV2"])
        self.assertTrue(all(s.direct for s in streams))

    def test_relative_url_is_completed(self):
        body = "#EXTM3U\n#EXTINF:-1,CCTV1\n/udp/239.1.1.1:1234\n"
        streams = self._collect(body.encode())
        self.assertEqual(streams[0].url, "http://nas:7080/udp/239.1.1.1:1234")

    def test_manual_lines_skip_network(self):
        client = FakeClient()
        cfg = {"name": "手填", "lines": "台1,http://a/udp/1:1", "mode": "direct"}
        streams = CustomCollector(name="手填", config=cfg).collect(client)
        self.assertEqual(len(streams), 1)
        self.assertEqual(client.asked, [], "手填列表不该再发请求")
        self.assertTrue(streams[0].direct)

    def test_force_direct_applies_to_playlist(self):
        streams = self._collect(M3U_MIXED.encode(), mode="direct")
        self.assertTrue(all(s.direct for s in streams))


class PipelineHookTest(unittest.TestCase):
    def test_direct_streams_get_no_slug(self):
        direct = Stream(url="http://nas:7080/udp/239.1.1.1:1234", custom=True, direct=True)
        relay = Stream(url="http://cdn.example.com/a.m3u8", custom=True)
        _route_through_relay([direct, relay])
        self.assertEqual(direct.slug, "", "裸流被包成 /live/ 就打不开了")
        self.assertTrue(relay.slug)

    def test_quality_gate_skips_custom(self):
        settings = SimpleNamespace(
            min_height=720, max_height=2160, min_speed_mbps=0.8,
            allow_unknown=False, speed_ratio=0.7,
        )
        low = Stream(url="http://nas:7080/udp/1", custom=True, ok=True, height=0)
        normal = Stream(url="http://cdn/a.m3u8", ok=True, height=0)
        kept = _filter([low, normal], settings)
        self.assertEqual([s.url for s in kept], [low.url])


class ClassifyTest(unittest.TestCase):
    def test_playlist(self):
        client = FakeClient(body=M3U_MIXED.encode(), url="http://nas:7080/playlist.m3u")
        info = custom.classify(client, "http://nas:7080/playlist.m3u")
        self.assertTrue(info["ok"])
        self.assertEqual(info["kind"], "playlist")
        self.assertEqual(info["channels"], 2)
        self.assertEqual((info["relay"], info["direct"]), (1, 1))

    def test_raw(self):
        client = FakeClient(body=b"\x47" + b"\x00" * 2000)
        info = custom.classify(client, "http://nas:7080/udp/239.1.1.1:1234")
        self.assertTrue(info["ok"])
        self.assertEqual(info["kind"], "raw")

    def test_http_error_is_reported(self):
        client = FakeClient(body=b"", status=502)
        info = custom.classify(client, "http://nas:7080/playlist.m3u")
        self.assertFalse(info["ok"])
        self.assertIn("502", info["error"])


class InjectTest(unittest.TestCase):
    def test_as_source_dicts_carries_enabled(self):
        items = [
            custom.CustomSource(name="开着的", url="http://a/1.m3u8"),
            custom.CustomSource(name="关着的", url="http://a/2.m3u8", enabled=False),
        ]
        rows = custom.as_source_dicts(items)
        self.assertEqual([r["type"] for r in rows], ["custom", "custom"])
        self.assertEqual([r["enabled"] for r in rows], [True, False])


if __name__ == "__main__":
    unittest.main()
