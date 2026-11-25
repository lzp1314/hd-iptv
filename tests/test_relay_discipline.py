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
    _RETRY_DELAYS,
    _STALE_MAX,
    _TICKET_BUDGET,
    _TICKET_REUSE,
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
            return body, {}, b"", url
        return 200, {}, body, url


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
        """下发的 MEDIA-SEQUENCE 必须与清单内容对应，且**只增不减**。

        上游序号会因为换 CDN 域名而回退 1~2 片（实测）。如果照着下发，播放器
        会认为这份清单比手里的旧，直接丢在一边并卡在 buffering——ExoPlayer
        #2872 的结论是「永不恢复」，正是「卡住不动」那个现象。
        """
        bodies = [_playlist(seq) for seq in range(100, 112)]
        relay, _ = _relay(bodies)
        seen = []
        for body in bodies:
            relay._absorb("cctv1", body.decode(), "https://cdn.example.com/live/")
            window = relay._windows["cctv1"]
            text = window.text()
            seq = window.first_out
            self.assertIn(f"#EXT-X-MEDIA-SEQUENCE:{seq}", text)
            seen.append(seq)
        self.assertEqual(seen, sorted(seen), "对外序号不能回退")

    def test_sequence_regression_does_not_rewind_output(self):
        """上游序号回退（换了 CDN 域名）时，对外序号必须原地不动，不能跟着退。"""
        relay, _ = _relay([_playlist(100), _playlist(99)])
        base = "https://cdn.example.com/live/"
        relay._absorb("cctv1", _playlist(100).decode(), base)
        before = relay._windows["cctv1"].first_out
        relay._absorb("cctv1", _playlist(99).decode(), base)
        self.assertGreaterEqual(
            relay._windows["cctv1"].first_out, before, "序号倒退会让播放器卡在 buffering"
        )

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
    """403 后只安静几秒，不能整台冷却半分钟。

    实测央视频的清单地址连取 2~5 次就开始 403、换张新票立刻恢复。旧实现
    一拒就冷却 30 秒，而窗口里只有约 30 秒的分片——冷却期刚好把画面耗干，
    这才是「播十几秒定住」的根因。这里锁住「退避必须短于窗口时长」。
    """

    def test_forbidden_backs_off_briefly(self):
        relay, fetch = _relay(403)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertIn("cctv1", relay._next_try)
        remaining = relay._next_try["cctv1"] - time.time()
        self.assertLessEqual(remaining, _RETRY_DELAYS[0] + 1.0)
        self.assertLess(
            _RETRY_DELAYS[0] * _WINDOW_SEGMENTS,
            _WINDOW_SEGMENTS * 5.0,
            "退避首档要明显短于窗口里那几片能撑的时间",
        )

        calls = fetch.calls
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertEqual(fetch.calls, calls, "退避期内一次都不该再打上游")

    def test_backoff_stays_within_player_buffer(self):
        """连着被拒时逐次拉长，但**必须封顶在播放器缓冲以内**（实测约 8 秒）。

        排到 10、18 秒时画面早就耗干了，用户看到的是「播一分多钟转圈九秒、
        然后彻底定住」。这条守住的是「退避不许长过缓冲」。
        """
        relay, _ = _relay(403)
        delays = []
        for _ in range(len(_RETRY_DELAYS) + 2):
            with relay._lock:
                relay._next_try.pop("cctv1", None)
            with self.assertRaises(RelayError):
                relay.playlist("cctv1")
            delays.append(round(relay._next_try["cctv1"] - time.time()))
        self.assertLessEqual(delays[0], 2, "第一轮要快，别让画面先等")
        self.assertLessEqual(max(delays), 6, "退避不能超过播放器缓冲（约 8 秒）")

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
            relay._windows["cctv1"].at = time.time() - (_STALE_MAX + 15)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")

    def test_last_known_never_refuses_by_age(self):
        """再旧也交得出来——HTTP 层靠它避免把 5xx 甩给播放器。

        ExoPlayer 重试 5 次后会抛错停住、libVLC 直接放弃这个流，两者都不会再
        回来问第二次。发一份过期清单，播放器顶多是取分片失败后重问清单；发
        5xx，它就不问了。
        """
        relay, _ = _relay(_playlist(100))
        relay.playlist("cctv1")
        with relay._lock:
            relay._windows["cctv1"].at = time.time() - (_STALE_MAX + 600)
        out = relay.last_known("cctv1")
        self.assertIsNotNone(out)
        text, stale, _seq = out
        self.assertTrue(stale)
        self.assertIn("#EXTM3U", text)

    def test_last_known_empty_when_nothing_cached(self):
        relay, _ = _relay(_playlist(100))
        self.assertIsNone(relay.last_known("never-played"))


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


class DnsErrorTest(unittest.TestCase):
    """解析失败不是上游失败，不该按 20 秒冷却。

    这是 0.9.10 在 NAS 上的实际事故：解析闸门被占满后所有台报「DNS 解析超时」，
    而这条错误又进了上游失败冷却——用户点一次黑屏，20 秒内再点还是同一句，
    看起来像永久坏了，实际上只要重新解析就能好。
    """

    def test_dns_error_is_retried_immediately(self):
        calls = []

        def broken(slug):
            calls.append(1)
            raise RuntimeError("接口无响应（DNS 解析超时）")

        relay = Relay(broken, warm=False)
        relay._fetch = _Fetch(_playlist(100))
        for _ in range(3):
            with self.assertRaises(RelayError):
                relay.playlist("dnsflap")
        self.assertEqual(len(calls), 3, "解析失败要能立刻重试，不能被冷却挡住")

    def test_upstream_error_still_cools_down(self):
        calls = []

        def broken(slug):
            calls.append(1)
            raise RuntimeError("上游 502")

        relay = Relay(broken, warm=False)
        relay._fetch = _Fetch(_playlist(100))
        for _ in range(3):
            with self.assertRaises(RelayError):
                relay.playlist("upsflap")
        self.assertEqual(len(calls), 1, "上游真报错时要冷却，别把接口敲穿")


class PlaylistContractTest(unittest.TestCase):
    """发给播放器的清单要自己说清「从哪儿开始播」。

    外挂播放器（VLC / 观屿）默认从清单第一片开始，窗口是 6 片就等于凭空
    多落后 30 秒。EXT-X-START 是 HLS 标准标签，ExoPlayer、VLC 都认。
    """

    def test_playlist_tells_player_to_start_near_live(self):
        relay, _ = _relay(_playlist(100))
        relay.playlist("cctv1")
        base = "https://cdn.example.com/live/index.m3u8"
        for seq in (101, 102, 103):
            relay._absorb("cctv1", _playlist(seq).decode(), base)
        text = relay.playlist("cctv1")
        self.assertIn("#EXT-X-START:TIME-OFFSET=-", text)
        offset = float(text.split("#EXT-X-START:TIME-OFFSET=")[1].split("\n")[0])
        self.assertLess(offset, 0, "TIME-OFFSET 必须是负的（从末尾往前数）")
        self.assertLessEqual(
            len([ln for ln in text.splitlines() if ln.startswith("http")]),
            _WINDOW_SEGMENTS,
            "窗口不能超出片数上限，否则首片会被 CDN 回收",
        )

    def test_short_window_has_no_start_tag(self):
        """只有 3 片时别写这个标签：那等于让播放器从第一片开始，多此一举。"""
        relay, _ = _relay(_playlist(100))
        text = relay.playlist("cctv1")
        self.assertNotIn("#EXT-X-START", text)


class TicketReuseTest(unittest.TestCase):
    """央视频的票据只能用 2 次，第 3 次必 403。

    所以要在**用满之前**主动换票，而不是抱着同一张一直用、等它被拒。只按
    ``addr_ttl``（300 秒）复用的话，后台每 5 秒取一次清单，第 10 秒就必然撞
    403，撞完还要退避 1~5 秒，几次叠加就是 20~30 秒断档——窗口总共就 30 秒
    内容，正好被耗干，于是「播一分多钟卡住、不自恢复」。
    """

    def _relay_with(self, host):
        calls = []

        def resolver(slug):
            calls.append(slug)
            return f"https://{host}/live/index.m3u8?t={len(calls)}", "qqlive"

        return Relay(resolver, warm=False), calls

    def test_ysp_ticket_rotates_after_two_uses(self):
        relay, calls = self._relay_with("live-dtocnc-cdn.ysp.cctv.cn")
        relay.upstream("cctv1")   # 冷启动，向接口要第一张票
        relay.upstream("cctv1")   # 第二次，复用同一张
        relay.upstream("cctv1")   # 已经用满 2 次：主动换票，不等它 403
        self.assertEqual(len(calls), 2, "第 3 次必须换票，否则这里就是一次 403")

    def test_other_hosts_keep_reusing(self):
        """运营商源的一张票能拉很多次，不能跟着央视频的节奏换，那会烧光换票预算。"""
        relay, calls = self._relay_with("cdn.example.com")
        for _ in range(5):
            relay.upstream("cctv1")
        self.assertEqual(len(calls), 1)

    def test_no_403_when_tickets_rotate(self):
        """按「用满 2 次就换」跑 20 轮，一次 403 都不该撞上。

        假的 CDN 严格复现实测规则：同一个地址第 1、2 次 200，第 3 次起 403。
        改之前（只按 300 秒 TTL 复用）这里会稳定撞出一串 403，每次还要退避
        1~5 秒——那正是 20~30 秒断档的来源。
        """
        counter = {"n": 0}
        seen = {}

        def resolver(slug):
            counter["n"] += 1
            return f"https://live-dtocnc-cdn.ysp.cctv.cn/live/index.m3u8?t={counter['n']}", "qqlive"

        relay = Relay(resolver, warm=False)
        fetch = _Fetch(_playlist(100))
        relay._fetch = fetch

        def fake_fetch(url, ua, *, timeout=None):
            n = seen[url] = seen.get(url, 0) + 1
            if n > _TICKET_REUSE:
                return 403, {}, b"", url
            return fetch(url, ua, timeout=timeout)

        relay._fetch = fake_fetch
        # 取票预算（burst 10）在这里不补，跑 8 轮足够覆盖「同一张票被复用 2 次」
        # 和「用满就换」两个分支，又不至于被预算拦住。
        for _ in range(8):
            relay._prefetch("cctv1")

        self.assertEqual(relay.failures, 0, "主动换票之后不该再撞 403")
        self.assertEqual(relay.stale_served, 0, "清单一直在推进，不需要拿旧清单顶")


if __name__ == "__main__":
    unittest.main()
