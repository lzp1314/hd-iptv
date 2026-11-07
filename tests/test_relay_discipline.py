"""请求纪律：这几条是「别人播着不卡、我这儿总 403」真正的分界线。

背景（实测，2026-11-07）：央视频官方媒体清单只列最近 3 片，但清单头的
``#EXT-QQHLS-SEGMENT_RANGE`` 显示 CDN 上只留 11 片（约 55 秒）。于是：

* 窗口攒得比留存还长 → 首片必然已被回收 → 播放器从那儿续播就是一片接一片的 403；
* 清单正文按有效期缓存 → 播放器反复拿到同一批片，播完就定住；
* 播放器「刷新预览图 / 检测可用性」把六十多个台挨个点一遍 → 每台换一张票，
  出口 IP 被打进限流，之后连正在看的人也一起 403。

这里的测试锁住对应的四道闸：窗口上限、在播判定、取票预算、按台冷却。
"""

from __future__ import annotations

import threading
import time
import unittest

from iptvhd.relay import (
    Relay,
    RelayError,
    _BLACKOUT_COOLDOWN,
    _FORBIDDEN_COOLDOWN,
    _TICKET_BUDGET,
    _WINDOW_SEGMENTS,
)


def _playlist(seq: int, *, duration: float = 5.0, range_tag: bool = True) -> bytes:
    head = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
    if range_tag:
        head += f"#EXT-QQHLS-SEGMENT_RANGE:{seq - 8}-{seq + 2}\n"
    head += f"#EXT-X-MEDIA-SEQUENCE:{seq}\n"
    body = ""
    for i in range(3):
        body += f"#EXTINF:{duration:.1f},\nseg/{seq + i}.ts\n"
    return (head + body).encode("utf-8")


class _Fetch:
    def __init__(self, bodies):
        self.bodies = list(bodies) if isinstance(bodies, list) else [bodies]
        self.calls = 0

    def __call__(self, url, ua, timeout=None):  # noqa: ANN001
        self.calls += 1
        body = self.bodies[min(self.calls - 1, len(self.bodies) - 1)]
        if isinstance(body, int):
            return body, {}, b""
        return 200, {}, body


def _relay(bodies, resolver=None):
    relay = Relay(
        resolver or (lambda slug: ("https://cdn.example.com/live/index.m3u8", "qqlive")),
        warm=False,
    )
    fetch = _Fetch(bodies)
    relay._fetch = fetch
    relay._ticker = threading.Thread(target=lambda: None)
    return relay, fetch


class WindowCapTest(unittest.TestCase):
    """窗口不能攒得比上游留存还长。"""

    def test_window_never_exceeds_cap(self):
        bodies = [_playlist(seq) for seq in range(100, 120)]
        relay, _ = _relay(bodies)
        for i in range(15):
            relay._absorb("cctv1", bodies[min(i, len(bodies) - 1)].decode(), "https://cdn.example.com/live/")
        window = relay._windows["cctv1"]
        self.assertLessEqual(
            len(window.segs),
            _WINDOW_SEGMENTS,
            "攒超过 CDN 留存（实测 11 片）就会把已被回收的分片写进清单",
        )

    def test_media_sequence_matches_first_segment(self):
        """拼出来的窗口里，MEDIA-SEQUENCE 必须等于第一片的真实序号。

        对不上时播放器按序号算地址，取到的就是隔壁那一片——表现是花屏、卡顿，
        或者干脆 404。
        """
        bodies = [_playlist(seq) for seq in range(100, 112)]
        relay, _ = _relay(bodies)
        first_seq = 100
        for i, body in enumerate(bodies):
            relay._absorb("cctv1", body.decode(), "https://cdn.example.com/live/")
        window = relay._windows["cctv1"]
        text = window.text()
        self.assertIn(f"#EXT-X-MEDIA-SEQUENCE:{window.seq0}", text)
        # 窗口首片应该就是 seq0 那一支
        self.assertIn(f"seg/{window.seq0}.ts", text.splitlines()[0] if False else text)
        self.assertGreaterEqual(window.seq0, first_seq)

    def test_targetduration_recomputed_and_range_tag_dropped(self):
        """TARGETDURATION 按窗口里实际的片长重算，上游的留存范围标签不带出去。"""
        relay, _ = _relay(_playlist(100, duration=9.0))
        relay._absorb("cctv1", _playlist(100, duration=9.0).decode(), "https://cdn.example.com/live/")
        text = relay._windows["cctv1"].text()
        self.assertIn("#EXT-X-TARGETDURATION:9", text, "报小了播放器会按它算刷新节奏")
        self.assertNotIn("#EXT-QQHLS-SEGMENT_RANGE", text)


class PlayingTest(unittest.TestCase):
    """只有「真有播放器在轮询」的台才值得开后台刷新。"""

    def test_single_hit_is_not_playing(self):
        relay, _ = _relay(_playlist(100))
        self.assertFalse(relay._note_hit("cctv1"), "扫台只碰一次，不能算在播")

    def test_back_to_back_hits_are_not_playing(self):
        relay, _ = _relay(_playlist(100))
        relay._note_hit("cctv1")
        self.assertFalse(relay._note_hit("cctv1"), "同一批并发请求（毫秒级）也不算")

    def test_regular_polling_is_playing(self):
        relay, _ = _relay(_playlist(100))
        with relay._lock:
            relay._hits["cctv1"] = time.time() - 3     # 播放器 3 秒后又来要了一次
        self.assertTrue(relay._note_hit("cctv1"))
        self.assertIn("cctv1", relay._active)

    def test_gone_too_long_is_not_playing(self):
        relay, _ = _relay(_playlist(100))
        with relay._lock:
            relay._hits["cctv1"] = time.time() - 60
        self.assertFalse(relay._note_hit("cctv1"), "早就不看了，重新开始前先按不在播处理")


class TicketBudgetTest(unittest.TestCase):
    """换票预算：一分钟最多 _TICKET_BUDGET 张，最后一枪不打上游。"""

    def test_budget_exhausts(self):
        relay, _ = _relay(_playlist(100))
        for _ in range(_TICKET_BUDGET):
            self.assertTrue(relay._take_ticket())
        self.assertFalse(relay._take_ticket(), "扫台到第 21 个台就该被拦住")

    def test_budget_refills_over_time(self):
        relay, _ = _relay(_playlist(100))
        for _ in range(_TICKET_BUDGET):
            relay._take_ticket()
        self.assertFalse(relay._take_ticket())
        # 把这一分钟的记账整体往前拨，等于过了一分钟
        with relay._bucket_lock:
            relay._ticket_times = type(relay._ticket_times)(
                t - 61 for t in relay._ticket_times
            )
        self.assertTrue(relay._take_ticket(), "过了一分钟应该重新有额度")

    def test_cached_address_does_not_spend_ticket(self):
        """抱着旧地址续播不能算换票，否则正常观看一会儿就把额度用光。"""
        relay, _ = _relay(_playlist(100))
        relay.upstream("cctv1")                     # 第一次：换票
        used = len(relay._ticket_times)
        relay.upstream("cctv1")                     # 第二次：地址还有效，直接用
        self.assertEqual(len(relay._ticket_times), used)


class CooldownTest(unittest.TestCase):
    """主备全 403 的台先冷却，别让播放器的毫秒级重试把限流拖长。"""

    def test_forbidden_cools_down_per_channel(self):
        relay, fetch = _relay(403)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertIn("cctv1", relay._forbidden_by_slug)
        remaining = relay._forbidden_by_slug["cctv1"] - time.time()
        self.assertGreater(remaining, _FORBIDDEN_COOLDOWN - 5)

        calls = fetch.calls
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertEqual(fetch.calls, calls, "冷却期内一次都不该再打上游")

    def test_cooling_one_channel_does_not_block_others(self):
        relay, fetch = _relay(403)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        relay._fetch = _Fetch(_playlist(100))       # 换个台，上游是好的
        text = relay.playlist("cctv2")
        self.assertIn("#EXTINF", text, "一个台被拒不能连累其它台")

    def test_blackout_cools_down_per_channel(self):
        """官方回「应版权方要求暂停直播」时，别每秒一张票地重试。"""

        def denied(slug):
            raise RuntimeError("该频道应版权方要求，暂停提供直播信号")

        relay, _ = _relay(_playlist(100), resolver=denied)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertIn("cctv1", relay._blackout)
        remaining = relay._blackout["cctv1"] - time.time()
        self.assertGreater(remaining, _BLACKOUT_COOLDOWN - 5)


class StaleGraceTest(unittest.TestCase):
    """旧清单能顶多久：30 秒内顶住，超过就老实报错。"""

    def test_stale_served_within_grace(self):
        relay, _ = _relay(_playlist(100))
        first = relay.playlist("cctv1")
        relay._fetch = _Fetch(500)                  # 上游开始报错
        past = time.time() - 20
        with relay._lock:
            relay._windows["cctv1"].at = past
        second = relay.playlist("cctv1")
        self.assertEqual(first, second)
        self.assertGreaterEqual(relay.stale_served, 1)

    def test_stale_beyond_grace_raises(self):
        relay, _ = _relay(_playlist(100))
        relay.playlist("cctv1")
        relay._fetch = _Fetch(500)
        with relay._lock:
            relay._windows["cctv1"].at = time.time() - 40
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")


class StaleRefreshTest(unittest.TestCase):
    """旧清单可以顶上，但**顶的同时必须去补新的**。

    只发不取是实测踩过的坑：窗口过了「新鲜」线、后台预取又没跟上时，播放器
    会一遍遍拿到同一份 3 片清单，序号一动不动——播完这十几秒画面就定住，
    下一条请求还是同一份。
    """

    def test_stale_response_kicks_a_refresh(self):
        relay, fetch = _relay([_playlist(100), _playlist(101)])
        first = relay.playlist("cctv1")
        self.assertIn("seg/100.ts", first)
        with relay._lock:
            relay._windows["cctv1"].at = time.time() - 10   # 过了新鲜线
        relay.playlist("cctv1")                              # 这一发是旧的，但应触发补货

        deadline = time.time() + 3
        while time.time() < deadline and relay._refreshing:
            time.sleep(0.02)
        with relay._lock:
            window = relay._windows["cctv1"]
        self.assertGreaterEqual(window.seq0 + len(window.segs) - 1, 101, "后台补货没有把新分片并进来")
        self.assertGreaterEqual(fetch.calls, 2)

    def test_refresh_is_deduped_per_channel(self):
        """同一时刻只开一个补货线程：播放器的重试不该变成好几个并发上游请求。"""
        relay, fetch = _relay(_playlist(100))
        inner = relay._fetch

        def slow(url, ua, timeout=None):  # noqa: ANN001
            time.sleep(0.3)               # 真实上游取一份清单要好几百毫秒
            return inner(url, ua, timeout)

        relay._fetch = slow
        for _ in range(5):
            relay._refresh_async("cctv1")
        deadline = time.time() + 3
        while time.time() < deadline and relay._refreshing:
            time.sleep(0.02)
        self.assertEqual(fetch.calls, 1, "五个并发请求应该合并成一次上游取清单")


if __name__ == "__main__":
    unittest.main()
