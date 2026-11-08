"""中转层测试：改写、放行名单、失败处理。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from iptvhd.relay import Relay, RelayError, allowed, decode, encode, parse_range
from iptvhd.models import Channel, Stream
from iptvhd.writers import write_m3u


def _expire(relay: Relay, slug: str = "cctv1", seconds: float = 20.0) -> None:
    """把缓存时间往前拨，逼下一次请求必须去上游。

    正常播放时窗口是被后台预取一直刷新着的，测试里没有那个线程，
    就手动让它「已经放了很久」。
    """
    past = time.time() - seconds
    window = relay._windows.get(slug)
    if window is not None:
        window.at = past
    raw = relay._raw.get(slug)
    if raw is not None:
        relay._raw[slug] = (past, raw[1], raw[2], raw[3])

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:4.000,
seg-1.ts
#EXTINF:4.000,
https://hlsliveali-cdn.ysp.cctv.cn/seg-2.ts
"""


class FakeRelay(Relay):
    """不联网的 Relay：把上游响应写死。"""

    def __init__(self, pages: dict[str, tuple[int, str]], **options):
        super().__init__(
            lambda slug: ("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/1.m3u8", "qqlive"), **options
        )
        self.pages = pages
        self.asked: list[str] = []

    def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
        self.asked.append(url)
        status, text = self.pages.get(url.rsplit("/", 1)[-1], (404, ""))
        return status, {"Content-Type": "application/vnd.apple.mpegurl"}, text.encode()


class CountingRelay(Relay):
    """解析次数可数的替身，用来验证「不按时间主动重解析」。"""

    def __init__(self):
        self.calls = 0
        super().__init__(self._resolver, warm=False)

    def _resolver(self, slug):
        self.calls += 1
        return "https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/index.m3u8", "qqlive"

    def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001
        return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, MASTER.encode()


class FlakyResolver(Relay):
    """解析必失败，验证冷却期内不会反复敲门。"""

    def __init__(self):
        self.calls = 0
        super().__init__(self._resolver)

    def _resolver(self, slug):
        self.calls += 1
        raise RuntimeError("iretcode=14")


class RescuingRelay(Relay):
    """第一次分片被拒，第二次放行，验证重试。"""

    def __init__(self):
        super().__init__(lambda slug: ("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/1.m3u8", "qqlive"))
        self.attempts = 0

    def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001
        self.attempts += 1
        if self.attempts == 1:
            return 403, {}, b""
        return 200, {"Content-Type": "video/mp2t"}, b"TS-DATA"


class RelayTest(unittest.TestCase):
    def test_allowlist(self):
        self.assertTrue(allowed("https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8"))
        self.assertTrue(allowed("https://live-dtocnc-cdn.ysp.cctv.cn/A/1.m3u8"))
        self.assertTrue(allowed("https://ali-xwl.cztv.com/live/x.m3u8"))
        self.assertFalse(allowed("https://example.com/a.ts"))
        self.assertFalse(allowed("https://cctv.cn.example.com/a.ts"))
        self.assertFalse(allowed("ftp://hlsliveali-cdn.ysp.cctv.cn/a.ts"))
        self.assertFalse(allowed(""))

    def test_encode_roundtrip(self):
        url = "https://hlsliveali-cdn.ysp.cctv.cn/A/seg-1.ts?a=1&b=2"
        self.assertEqual(decode(encode(url)), url)
        self.assertNotIn("=", encode(url))  # 放进路径里不能带填充符
        self.assertEqual(decode("!!!not-base64!!!"), "")

    def test_playlist_direct_by_default(self):
        """默认 direct：清单里换成上游绝对地址，分片让播放器直连 CDN。

        实测全代理时本机去拉分片会被平台回 403（每片一次裸请求，被判成异常
        流量），只有清单经本机、分片直连才稳定——所以这是默认，不能随手改回去。
        """
        relay = FakeRelay({"1.m3u8": (200, MASTER), "index.m3u8": (200, MEDIA)})
        text = relay.playlist("cctv1")
        self.assertIn("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/index.m3u8", text)
        self.assertNotIn("/relay/", text)
        self.assertNotIn("\nindex.m3u8", text)  # 相对地址必须换成绝对地址
        self.assertEqual(relay.stats()["mode"], "direct")

    def test_proxy_mode_rewrites_everything(self):
        """切成 proxy 才把分片也换成本机地址（给直连放不出来的播放器兜底）。"""
        relay = FakeRelay({"1.m3u8": (200, MASTER), "index.m3u8": (200, MEDIA)}, mode="proxy")
        text = relay.playlist("cctv1")
        self.assertIn("/relay/cctv1/p/", text)
        self.assertNotIn("ysp.cctv.cn", text)  # 播放器不该看到上游域名

        token = text.split("/relay/cctv1/p/")[1].split("\n")[0].strip()
        self.assertTrue(decode(token).startswith("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/"))
        code, ctype, body, _extra = relay.resource("cctv1", token)
        media = body.decode()
        self.assertEqual(code, 200)
        self.assertIn("mpegurl", ctype)
        self.assertNotIn("ysp.cctv.cn", media)
        self.assertIn('URI="/relay/cctv1/p/', media)
        self.assertEqual(media.count("/relay/cctv1/p/"), 3)  # 密钥 + 2 个分片

    def test_proxy_mode_uses_absolute_urls_when_prefix_given(self):
        """proxy 模式给了对外地址就写全路径：部分播放器不解析相对路径。"""
        relay = FakeRelay({"1.m3u8": (200, MASTER), "index.m3u8": (200, MEDIA)}, mode="proxy")
        text = relay.playlist("cctv1", "http://192.168.1.10:9797")
        self.assertIn("http://192.168.1.10:9797/relay/cctv1/p/", text)

        token = text.split("/relay/cctv1/p/")[1].split("\n")[0].strip()
        _, _, body, _ = relay.resource("cctv1", token, "", "http://tv.example.com:8443")
        media = body.decode()
        self.assertIn('URI="http://tv.example.com:8443/relay/cctv1/p/', media)
        # 内网、外网两套地址各自缓存，别互相顶掉
        self.assertEqual(len(relay.stats()["playlists"]), 1)

    def test_switch_mode_clears_cached_playlists(self):
        """切换播放方式要清掉已改写的清单，否则面板改了设置却不生效。"""
        relay = FakeRelay({"1.m3u8": (200, MASTER), "index.m3u8": (200, MEDIA)})
        self.assertNotIn("/relay/", relay.playlist("cctv1"))
        relay.configure(mode="proxy")
        self.assertIn("/relay/", relay.playlist("cctv1"))

    def test_playlist_retries_once_on_403(self):
        relay = FakeRelay({"1.m3u8": (403, "")})
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertEqual(len(relay.asked), 2)  # 首次 + 换地址后重试

    def test_playlist_rejects_non_m3u8(self):
        relay = FakeRelay({"1.m3u8": (200, "<html>403</html>")})
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")

    def test_resource_blocks_foreign_host(self):
        relay = FakeRelay({})
        with self.assertRaises(RelayError):
            relay.resource("cctv1", encode("https://example.com/a.ts"))

    def test_resource_rejects_bad_token(self):
        relay = FakeRelay({})
        with self.assertRaises(RelayError):
            relay.resource("cctv1", "%%%")


class WriterRelayTest(unittest.TestCase):
    def test_relay_base_switches_to_local_url(self):
        stream = Stream(url="https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8", slug="cctv1",
                        user_agent="qqlive", height=1080)
        ch = Channel(name="CCTV-1", group="央视", streams=[stream])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.m3u"
            write_m3u([ch], path, relay_base="http://192.168.1.10:9797/")
            text = path.read_text(encoding="utf-8")
        self.assertIn("http://192.168.1.10:9797/live/cctv1.m3u8", text)
        self.assertNotIn("EXTVLCOPT", text)  # 走中转就不需要指定 UA 了
        self.assertNotIn("ysp.cctv.cn", text)

    def test_without_relay_base_gives_relative_path(self):
        """没有对外地址时给相对路径，而不是把抽测轮换来的票写死进列表。

        写死的票据几小时后就失效，播放器那边表现为「能选中、打开黑屏」。
        """
        stream = Stream(url="https://hlsliveali-cdn.ysp.cctv.cn/A/1.m3u8", slug="cctv1",
                        user_agent="qqlive", height=1080)
        ch = Channel(name="CCTV-1", group="央视", streams=[stream])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.m3u"
            write_m3u([ch], path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("/live/cctv1.m3u8", text)
        self.assertNotIn("ysp.cctv.cn", text)


class RelayPolicyTest(unittest.TestCase):
    def test_address_is_reused_not_refreshed(self):
        """曾经每 3 分钟重解析一次，几次就把接口敲到限流，这里锁死行为。"""
        relay = CountingRelay()
        relay.playlist("cctv1")
        relay.playlist("cctv1")
        relay.playlist("cctv1")
        self.assertEqual(relay.calls, 1)

    def test_address_is_renewed_after_ttl(self):
        """同一张票据用太久会被 CDN 拒（403），到期要主动换一张。"""
        import time

        relay = CountingRelay()
        relay._addr_ttl = 0.05
        relay.upstream("cctv1")
        self.assertEqual(relay.calls, 1)
        time.sleep(0.08)
        relay.upstream("cctv1")
        self.assertEqual(relay.calls, 2)

    def test_failed_resolve_enters_cooldown(self):
        relay = FlakyResolver()
        for _ in range(5):
            with self.assertRaises(RelayError):
                relay.upstream("cctv1")
        self.assertEqual(relay.calls, 1)  # 冷却期内只敲一次
        self.assertEqual(relay.failures, 1)

    def test_segment_retries_before_giving_up(self):
        relay = RescuingRelay()
        token = encode("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/seg-1.ts")
        code, ctype, body, _extra = relay.resource("cctv1", token)
        self.assertEqual(code, 200)
        self.assertEqual(body, b"TS-DATA")
        self.assertIn("mp2t", ctype)
        self.assertEqual(relay.attempts, 2)

    def test_budget_serves_stale_instead_of_hitting_upstream(self):
        """播放器批量扫台时，配额用完就绝不再撞上游。

        「一被限流连正在看的台也一起 403」的防线：请求量由本机控制，不由播放器
        的扫台节奏决定。数据还在 _STALE_MAX（30 秒）内时根本走不到配额这一步
        ——直接发手里的（见 test_stale_within_grace_keeps_serving）；这里测的是
        已经旧到没法再用、又没有配额的情况：如实报错，但一枪不打上游。
        """
        relay = CountingRelay()
        relay.playlist("cctv1")
        before = relay.calls
        # 拨过 _STALE_MAX（30 秒）：再旧的清单发出去也没用，官方 CDN 只留 11 片
        _expire(relay, seconds=40)                   # 缓存旧到不能再用，本来该去上游
        relay._take_token = lambda wait=0.0: False   # 配额用完
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertEqual(relay.calls, before)        # 没有额外解析
        self.assertEqual(relay.throttled, 1)

    def test_stats_shape(self):
        relay = CountingRelay()
        relay.playlist("cctv1")
        stats = relay.stats()
        self.assertEqual(stats["failures"], 0)
        self.assertIn("cctv1", stats["cached"])



class ToggleRelay(Relay):
    """可以被「弄断」的替身，验证上游失败时用旧清单兜住。"""

    def __init__(self, ttl: float = 0.0):
        super().__init__(
            lambda slug: ("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/1.m3u8", "qqlive"),
            prefetch_every=ttl,
            warm=False,
        )
        self.down = False
        self.fetches = 0

    def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001
        self.fetches += 1
        if self.down:
            raise RelayError("上游不可达：模拟断线")
        return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, MEDIA.encode()


class SegRelay(Relay):
    """分片替身，记录上游被请求了几次。"""

    def __init__(self):
        super().__init__(lambda slug: ("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/1.m3u8", "qqlive"))
        self.fetches = 0

    def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001
        self.fetches += 1
        return 200, {"Content-Type": "video/mp2t"}, b"0123456789"


class RelayStabilityTest(unittest.TestCase):
    def test_playlist_falls_back_to_last_good(self):
        """上游断一下不能把 5xx 甩给播放器——那等于让它跳到下一个台。"""
        relay = ToggleRelay()
        first = relay.playlist("cctv1")
        relay.down = True
        _expire(relay)
        second = relay.playlist("cctv1")
        self.assertEqual(first, second)
        self.assertEqual(relay.stale_served, 1)

    def test_playlist_raises_when_never_succeeded(self):
        relay = ToggleRelay()
        relay.down = True
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")

    def test_stale_within_grace_keeps_serving(self):
        """30 秒内上游取不到，继续发手里的清单，播放器看不到失败。

        官方 CDN 留 11 片（约 55 秒），30 秒内的分片地址播放器还取得到。所以
        这一段是可以硬顶的，顶住比让播放器跳台强得多。
        """
        relay = ToggleRelay()
        first = relay.playlist("cctv1")
        relay.down = True
        _expire(relay, seconds=20)     # 旧，但还在 _STALE_MAX 之内
        second = relay.playlist("cctv1")
        self.assertEqual(first, second)
        self.assertGreaterEqual(relay.stale_served, 1)

    def test_stale_beyond_grace_reports_failure(self):
        """超过 30 秒就老实报错。

        再旧的清单发出去也是死链：分片早被 CDN 回收，而且内容已经不是现在播的
        了，继续回旧清单只会让人以为还在播。到点报错，播放器自己重试或换台。
        """
        relay = ToggleRelay()
        relay.playlist("cctv1")
        relay.down = True
        _expire(relay, seconds=40)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")

    def test_segment_cached_between_requests(self):
        relay = SegRelay()
        token = encode("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/seg-1.ts")
        relay.resource("cctv1", token)
        relay.resource("cctv1", token)
        self.assertEqual(relay.fetches, 1)  # 第二次走缓存，不再打上游

    def test_range_request_returns_partial(self):
        relay = SegRelay()
        token = encode("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/seg-1.ts")
        code, _ctype, body, extra = relay.resource("cctv1", token, "bytes=2-4")
        self.assertEqual((code, body), (206, b"234"))
        self.assertEqual(extra["Content-Range"], "bytes 2-4/10")

    def test_range_out_of_bounds_is_416(self):
        relay = SegRelay()
        token = encode("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/seg-1.ts")
        code, _ctype, body, extra = relay.resource("cctv1", token, "bytes=100-200")
        self.assertEqual(code, 416)
        self.assertEqual(body, b"")
        self.assertTrue(extra["Content-Range"].endswith("/10"))

    def test_multi_range_gets_full_body(self):
        """多段范围不支持，但要回整段而不是 416——416 会让播放器直接放弃这条流。"""
        relay = SegRelay()
        token = encode("https://hlsliveali-cdn.ysp.cctv.cn/TOKEN/seg-1.ts")
        code, _ctype, body, extra = relay.resource("cctv1", token, "bytes=0-1,3-4")
        self.assertEqual((code, body), (200, b"0123456789"))
        self.assertEqual(extra, {})

    def test_parse_range(self):
        self.assertEqual(parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        self.assertIsNone(parse_range("", 100))
        self.assertIsNone(parse_range("bytes=200-300", 100))
        self.assertIsNone(parse_range("bytes=0-1,3-4", 100))

    def test_backoff_serves_stale_window_without_upstream(self):
        """限流退避期间，窗口过期了也别同步去上游（会被 403），先把手里的发给播放器。"""
        relay = ToggleRelay()
        first = relay.playlist("cctv1")
        self.assertEqual(relay.fetches, 1)
        # 进入退避：把这个台的下次可试时刻拨到未来
        relay._next_try["cctv1"] = time.time() + 60.0
        # 窗口过期（超过 _WINDOW_MAX_AGE）
        _expire(relay, seconds=20.0)
        fetches_before = relay.fetches
        text, stale, _seq = relay.playlist_info("cctv1")
        # 退避期间直接用了旧窗口，没有再去上游要
        self.assertEqual(relay.fetches, fetches_before)
        self.assertTrue(stale)
        self.assertIn("#EXTINF", text)

    def test_discontinuity_keeps_window_and_preserves_marker(self):
        """预告片/广告切正片时的 #EXT-X-DISCONTINUITY 不再让清单退化成 3 片原文。

        断点标记要原位保留在窗口输出里，播放器才知道「这里要跳一下」，而不是
        回跳到缓冲里残留的旧分片重播（用户看到的「又重复放」）。
        """
        from iptvhd.relay import _parse_media, _Window

        # 3 片里第 2 片前有个断点（预告片结束切回正片）
        text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:100
#EXTINF:5.000,
seg-100.ts
#EXT-X-DISCONTINUITY
#EXTINF:5.000,
seg-101.ts
#EXTINF:5.000,
seg-102.ts
"""
        head, seq, segs = _parse_media(text, "https://cdn.ysp.cctv.cn/TOKEN/")
        self.assertEqual(seq, 100)
        self.assertEqual(len(segs), 3)
        # 第 1 片无断点，第 2 片带断点
        self.assertFalse(segs[0][3])
        self.assertTrue(segs[1][3])
        self.assertFalse(segs[2][3])

        w = _Window()
        w.feed(head, seq, segs, time.time())
        out = w.text()
        # 断点标记被原位还原，且只出现在第 2 片前
        self.assertEqual(out.count("#EXT-X-DISCONTINUITY"), 1)
        self.assertLess(out.index("#EXT-X-DISCONTINUITY"), out.index("seg-101.ts"))
        self.assertGreater(out.index("#EXT-X-DISCONTINUITY"), out.index("seg-100.ts"))


if __name__ == "__main__":
    unittest.main()
