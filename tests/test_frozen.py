"""上游清单「冻住」的识别与换票重取。

真实场景：带 token 的清单地址不报错、也不是 403，就是反复返回同一批分片。
播放器把手里那十几秒播完就没有下一片，画面定住，服务端日志一片祥和。
所以 relay 盯 #EXT-X-MEDIA-SEQUENCE：超过 10 秒没往前滚就换票重取。
"""

from __future__ import annotations

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
    relay.configure(playlist_ttl=0.5)
    return relay, fetch


class TestFrozenPlaylist(unittest.TestCase):
    def test_normal_roll_is_not_frozen(self):
        relay, fetch = _relay([_playlist(100)])
        _text, stuck, seq = relay.playlist_info("cctv1")
        self.assertFalse(stuck)
        self.assertEqual(seq, 100)
        self.assertEqual(fetch.calls, 1)

    def test_sequence_advancing_keeps_resetting_timer(self):
        relay, _fetch = _relay([_playlist(100), _playlist(101)])
        relay.playlist_info("cctv1")
        time.sleep(0.55)
        _text, stuck, seq = relay.playlist_info("cctv1")
        self.assertFalse(stuck)
        self.assertEqual(seq, 101)
        key = "|cctv1"
        self.assertEqual(relay._seq_since[key][0], 101)

    def test_stuck_playlist_triggers_fresh_ticket(self):
        # 同一张票一直给 100；换票后给 200
        relay, fetch = _relay([_playlist(100), _playlist(100), _playlist(200)])
        relay.playlist_info("cctv1")
        self.assertEqual(fetch.calls, 1)

        # 模拟「这个序号已经 11 秒没动」——正常播放时 5 秒就该滚一片
        relay._seq_since["|cctv1"] = (100, time.time() - 11)
        time.sleep(0.55)
        text, stuck, seq = relay.playlist_info("cctv1")

        self.assertFalse(stuck, "换票换到了新序号，就该当作正常")
        self.assertEqual(seq, 200)
        self.assertEqual(fetch.calls, 3, "第 2 次取到旧序号后要再换票取一次")
        self.assertIn("200.ts", text)

    def test_frozen_after_fresh_ticket_is_reported(self):
        relay, fetch = _relay([_playlist(100)])
        notes = []
        relay.attach_log(notes.append)
        relay.playlist_info("cctv1")
        relay._seq_since["|cctv1"] = (100, time.time() - 11)
        time.sleep(0.55)

        text, stuck, seq = relay.playlist_info("cctv1")
        self.assertTrue(stuck, "换票也还是同一批分片，必须如实告诉调用方")
        self.assertEqual(seq, 100)
        self.assertIn("#EXTM3U", text)
        self.assertEqual(fetch.calls, 3, "第一次取 + 换票再取")
        self.assertTrue(any("冻" in n for n in notes), notes)

    def test_playlist_wrapper_returns_text(self):
        relay, _fetch = _relay([_playlist(100)])
        text = relay.playlist("cctv1")
        self.assertIsInstance(text, str)
        self.assertIn("#EXTM3U", text)

    def test_fetch_failure_still_falls_back_to_last_good(self):
        relay, _fetch = _relay([_playlist(100), 403])
        relay.playlist_info("cctv1")
        time.sleep(0.55)
        text, stale, seq = relay.playlist_info("cctv1")
        self.assertTrue(stale)
        self.assertEqual(seq, 100)
        self.assertIn("#EXTM3U", text)


if __name__ == "__main__":
    unittest.main()
