"""分片窗口与「上游冻住」的处理。

上游媒体清单只列 3 个分片（约 15 秒），播放器稍一落后就没片可播，画面就顿一下。
所以 relay 把每次拿到的分片攒成更长的窗口，并盯 ``#EXT-X-MEDIA-SEQUENCE``：
超过 10 秒没往前滚，说明这张票据冻住了，换一张重取。
"""

from __future__ import annotations

import threading
import time
import unittest

from iptvhd.relay import Relay


def _playlist(seq: int, tag: str = "a") -> bytes:
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:5\n"
        f"#EXT-X-MEDIA-SEQUENCE:{seq}\n"
        "#EXTINF:5.0,\n"
        f"{tag}/{seq}.ts\n"
        "#EXTINF:5.0,\n"
        f"{tag}/{seq + 1}.ts\n"
        "#EXTINF:5.0,\n"
        f"{tag}/{seq + 2}.ts\n"
    ).encode("utf-8")


MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
    "1080/index.m3u8\n"
)


class _Fetch:
    """假的「去上游取清单」：按顺序吐出排好的响应，并记下被要过几次。"""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def __call__(self, url, ua, timeout=None):
        self.calls += 1
        body = self.bodies[min(self.calls - 1, len(self.bodies) - 1)]
        if isinstance(body, int):
            return body, {}, b""
        return 200, {}, body


def _relay(bodies):
    relay = Relay(lambda slug: ("https://cdn.example.com/live/index.m3u8", "qqlive"))
    fetch = _Fetch(bodies)
    relay._fetch = fetch
    # 测试里不要真的起后台线程：它会在断言中间偷偷多发几次上游请求
    relay._ticker = threading.Thread(target=lambda: None)
    return relay, fetch


def _age_window(relay, slug="cctv1", *, moved: float = 0.0, fetched: float = 0.0) -> None:
    """把窗口的时间戳往前拨，模拟「已经过了这么久」。"""
    now = time.time()
    with relay._lock:
        window = relay._windows[slug]
        if moved:
            window.moved_at = now - moved
        if fetched:
            window.at = now - fetched


class TestWindow(unittest.TestCase):
    def test_first_request_builds_window(self):
        relay, fetch = _relay([_playlist(100)])
        text, stale, seq = relay.playlist_info("cctv1")

        self.assertFalse(stale)
        self.assertEqual(seq, 100)
        self.assertEqual(fetch.calls, 1)
        self.assertIn("https://cdn.example.com/live/a/100.ts", text)

    def test_live_requests_come_from_memory(self):
        relay, fetch = _relay([_playlist(100)])
        relay.playlist_info("cctv1")
        for _ in range(5):
            relay.playlist_info("cctv1")
        self.assertEqual(fetch.calls, 1, "窗口还有效，播放器来几次都不该再碰上游")

    def test_prefetch_extends_window(self):
        relay, _ = _relay([_playlist(100), _playlist(103)])
        relay.playlist_info("cctv1")
        relay._prefetch("cctv1")

        text, _, seq = relay.playlist_info("cctv1")
        self.assertEqual(seq, 100)
        self.assertEqual(text.count("#EXTINF"), 6, "3 片 + 3 片，拼成 30 秒")
        self.assertIn("100.ts", text)
        self.assertIn("105.ts", text)

    def test_window_is_capped(self):
        relay, _ = _relay([_playlist(100 + 3 * i) for i in range(6)])
        relay.playlist_info("cctv1")
        for _ in range(5):
            relay._prefetch("cctv1")

        text, _, _seq = relay.playlist_info("cctv1")
        self.assertEqual(text.count("#EXTINF"), 12, "窗口最多 12 片，不能无限长")
        self.assertIn("114.ts", text)
        self.assertNotIn("100.ts", text, "旧分片要滚出去，否则播放器可能取到已被回收的地址")

    def test_sequence_jump_resets_window(self):
        relay, _ = _relay([_playlist(100), _playlist(500)])
        relay.playlist_info("cctv1")
        relay._prefetch("cctv1")

        text, _, seq = relay.playlist_info("cctv1")
        self.assertEqual(seq, 500)
        self.assertEqual(text.count("#EXTINF"), 3, "序号接不上就以新清单为准，不能拼出错位的窗口")
        self.assertIn("502.ts", text)

    def test_master_playlist_is_passed_through(self):
        relay, _ = _relay([MASTER.encode()])
        text, _, seq = relay.playlist_info("cctv1")

        self.assertEqual(seq, -1)
        self.assertIn("#EXT-X-STREAM-INF", text)
        self.assertIn("https://cdn.example.com/live/1080/index.m3u8", text)


class TestPrefetch(unittest.TestCase):
    def test_sweep_prefetches_channel_being_watched(self):
        relay, fetch = _relay([_playlist(100), _playlist(103)])
        relay.playlist_info("cctv1")
        _age_window(relay, fetched=3)   # 超过预取间隔

        relay._sweep()

        self.assertEqual(fetch.calls, 2, "有人在看，后台就该替它去要新的")
        text, _, _ = relay.playlist_info("cctv1")
        self.assertEqual(text.count("#EXTINF"), 6)

    def test_sweep_skips_channel_nobody_watches(self):
        """没人看的台不能跟着一起刷——几十个台一起刷就是自己把出口 IP 敲到 403。"""
        relay, fetch = _relay([_playlist(100), _playlist(103)])
        relay.playlist_info("cctv1")
        _age_window(relay, fetched=3)
        with relay._lock:
            relay._active["cctv1"] = time.time() - 60

        relay._sweep()

        self.assertEqual(fetch.calls, 1)
        self.assertNotIn("cctv1", relay._active)


class TestFrozen(unittest.TestCase):
    def test_frozen_triggers_fresh_ticket(self):
        # 同一张票一直给 100；换票后给 200
        relay, fetch = _relay([_playlist(100), _playlist(200)])
        relay.playlist_info("cctv1")
        self.assertEqual(fetch.calls, 1)

        # 模拟「这个序号已经 11 秒没动」——正常播放时 5 秒就该滚一片
        _age_window(relay, moved=11)
        relay._prefetch("cctv1")

        text, _stale, seq = relay.playlist_info("cctv1")
        self.assertEqual(seq, 200, "换票换到了新序号，就该用新的")
        self.assertEqual(fetch.calls, 2)
        self.assertIn("200.ts", text)

    def test_frozen_is_reported(self):
        relay, _ = _relay([_playlist(100)])   # 换多少张票都是同一份
        notes = []
        relay.attach_log(notes.append)
        relay.playlist_info("cctv1")

        _age_window(relay, moved=31)
        relay._prefetch("cctv1")

        self.assertTrue(any("没往前滚" in n for n in notes), notes)

    def test_fetch_failure_falls_back_to_window(self):
        relay, _ = _relay([_playlist(100), 403, 403])
        relay.playlist_info("cctv1")

        # 窗口放旧，逼它必须去上游；上游这时是 403
        _age_window(relay, fetched=20)
        text, stale, seq = relay.playlist_info("cctv1")

        self.assertTrue(stale, "取不到新的就把旧窗口发出去，总比让播放器跳台好")
        self.assertEqual(seq, 100)
        self.assertIn("#EXTM3U", text)

    def test_playlist_wrapper_returns_text(self):
        relay, _ = _relay([_playlist(100)])
        text = relay.playlist("cctv1")
        self.assertIsInstance(text, str)
        self.assertIn("#EXTM3U", text)


if __name__ == "__main__":
    unittest.main()
