"""取源器与画质上限的测试。

这些用例不联外网：取源器的「生成地址」是纯函数逻辑，联网部分交给
probe 阶段和手工 `iptvhd probe` 验证。
"""

from __future__ import annotations

import unittest

from iptvhd import ysp
from iptvhd.generators import CctvCollector, TemplateCollector, YangshipinCollector, GENERATORS
from iptvhd.hls import guess_height
from iptvhd.models import Stream
from iptvhd.net import HttpClient
from iptvhd.pipeline import _filter
from iptvhd.settings import Settings


class TestCctvGenerator(unittest.TestCase):
    def test_covers_all_channels_across_lines(self):
        # collect 不联网，直接调用即可
        streams = CctvCollector(name="cctv").collect(None)
        self.assertEqual(len(streams), len(CctvCollector.CHANNELS) * len(CctvCollector.HOSTS))
        names = {s.raw_name for s in streams}
        self.assertIn("CCTV-1 综合", names)
        self.assertIn("CCTV-17 农业农村", names)

    def test_url_shape(self):
        streams = CctvCollector(name="cctv").collect(None)
        one = next(s for s in streams if s.raw_name.startswith("CCTV-5 ") and "txy" in s.url)
        self.assertTrue(one.url.endswith("/ldncctvwbcd/cdrmldcctv5_1/index.m3u8"))
        self.assertEqual(one.group, "央视")

    def test_only_filter(self):
        cfg = {"only": ["cctv1", "cctv13"]}
        streams = CctvCollector(name="cctv", config=cfg).collect(None)
        names = {s.raw_name for s in streams}
        self.assertEqual(names, {"CCTV-1 综合", "CCTV-13 新闻"})


class TestYangshipinGenerator(unittest.TestCase):
    """央视频取源器默认只登记频道，一个请求都不发。

    这是防「出口 IP 被限流」的核心：一轮全量取票（五十多个请求）打上去，
    之后所有台都会 403 几十分钟到几小时，播放器里就是「大半台放不了」。
    """

    def test_default_makes_no_request(self):
        def boom(*a, **k):  # noqa: ANN001
            raise AssertionError("默认不该向上游发请求")

        original = ysp.resolve
        ysp.resolve = boom
        try:
            streams = YangshipinCollector(name="ysp", config={}).collect(None)
        finally:
            ysp.resolve = original
        self.assertGreater(len(streams), 50)
        self.assertTrue(all(s.slug and not s.url for s in streams))
        # 剧场频道在上游只认 720P 档，其余都是 1080P
        self.assertTrue(all(s.ok and s.height in (1080, 720) for s in streams))

    def test_resolve_option_opt_in(self):
        """显式打开 resolve 才会去要地址——留给命令行做离线校验。"""
        calls = []

        def fake_resolve(cnlid, livepid, defn, client, **kw):  # noqa: ANN001
            calls.append(cnlid)
            return "https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8", {}, "https://hlsliveali-cdn.ysp.cctv.cn/A/1.ts"

        original = ysp.resolve
        ysp.resolve = staticmethod(fake_resolve)
        try:
            streams = YangshipinCollector(
                name="ysp", config={"resolve": True, "channels": ["cctv1"]}
            ).collect(HttpClient(user_agent="qqlive", timeout=5, retries=0))
        finally:
            ysp.resolve = original
        self.assertEqual(len(streams), 1)
        self.assertTrue(calls and streams[0].url.startswith("https://"))


class TestTemplateGenerator(unittest.TestCase):
    def test_expands_channels(self):
        cfg = {
            "url_template": "https://cdn.example.com/{code}/live.m3u8",
            "group": "卫视",
            "channels": [
                {"code": "hn", "name": "湖南卫视"},
                {"code": "zj", "name": "浙江卫视", "group": "自定"},
                {"name": "缺 code 的台"},  # 模板占位符缺失 → 跳过
            ],
        }
        streams = TemplateCollector(name="tpl", config=cfg).collect(None)
        self.assertEqual(len(streams), 2)
        by_name = {s.raw_name: s for s in streams}
        self.assertEqual(by_name["湖南卫视"].url, "https://cdn.example.com/hn/live.m3u8")
        self.assertEqual(by_name["浙江卫视"].group, "自定")

    def test_empty_template(self):
        self.assertEqual(TemplateCollector(name="x", config={}).collect(None), [])


class TestHeightDetection(unittest.TestCase):
    def test_loose_match_inside_word(self):
        # 严格边界匹配不到，宽松规则兜底
        self.assertEqual(guess_height("https://a/live/channel011080Pnew.m3u8"), 1080)
        self.assertEqual(guess_height("https://b/live/x720p_hd/index.m3u8"), 720)
        self.assertEqual(guess_height("https://c/live/channel4k2160p.m3u8"), 2160)

    def test_no_false_positive(self):
        self.assertEqual(guess_height("https://d/live/cctv1/index.m3u8"), 0)


class TestHeightCap(unittest.TestCase):
    def _settings(self, **probe):
        base = {"min_height": 720, "max_height": 1080, "min_speed_mbps": 1.5}
        base.update(probe)
        return Settings(root=__import__("pathlib").Path("."), data={"probe": base})

    def _stream(self, height: int, speed: float = 10.0) -> Stream:
        s = Stream(url=f"http://x/{height}.m3u8")
        s.ok = True
        s.height = height
        s.speed_mbps = speed
        return s

    def test_drops_4k_by_default(self):
        kept = _filter([self._stream(2160), self._stream(1080), self._stream(720)], self._settings())
        self.assertEqual([s.height for s in kept], [1080, 720])

    def test_cap_zero_keeps_everything(self):
        kept = _filter([self._stream(2160), self._stream(1080)], self._settings(max_height=0))
        self.assertEqual(len(kept), 2)

    def test_drops_below_min(self):
        kept = _filter([self._stream(540), self._stream(1080)], self._settings())
        self.assertEqual([s.height for s in kept], [1080])

    def test_drops_slow(self):
        kept = _filter([self._stream(1080, speed=0.3)], self._settings())
        self.assertEqual(kept, [])


class TestRegistry(unittest.TestCase):
    def test_generators_registered(self):
        from iptvhd.collectors import _full_registry

        registry = _full_registry()
        for kind in ("cctv", "zhejiang", "template"):
            self.assertIn(kind, registry)
            self.assertIn(kind, GENERATORS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
