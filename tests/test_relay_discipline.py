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
    _RETRY_DELAYS_DEAD,
    _STALE_MAX,
    _TICKET_BUDGET,
    _TICKET_TTL,
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

    def test_repeated_403_stops_rebuying_tickets(self):
        """连续被拒时不再「换票重试」：换票治不了出口 IP 的惩罚性限流。

        一张票用第 3 次必 403 是「票用旧了」，换票有效；但出口被限流时换票
        也是 100% 403。以前每轮失败都固定「睡 1 秒 + 换票 + 再取一次」，限流
        期等于请求数翻倍、换票预算双倍烧，还把我们自己按在惩罚期里出不来。
        """
        relay, fetch = _relay(403)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        first = fetch.calls
        self.assertEqual(first, 2, "第一次被拒值得换票再试一把")
        with relay._lock:
            relay._next_try.pop("cctv1", None)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertEqual(fetch.calls - first, 1, "连续第二次被拒就不再换票重试")

    def test_dead_window_backs_off_in_minutes(self):
        """窗口已经死透（分片全过期）时按分钟级退避，别续着上游的惩罚期。

        窗口还活着时短退避是对的（几秒内接上）；但分片全过期之后，无论多快
        重取都好不了，播放器只能报错重开——这时 5 秒封顶等于给「重开→再打
        上游→再被拒」这个循环发通行证。
        """
        relay, _ = _relay(_playlist(100))
        relay.playlist("cctv1")
        with relay._lock:
            relay._windows["cctv1"].at = time.time() - (_STALE_MAX + 15)
        relay._fetch = _Fetch(403)
        relay.playlist_info("cctv1")                # 拿不到新的，降级发最后一片
        remaining = relay._next_try.get("cctv1", 0.0) - time.time()
        self.assertGreaterEqual(
            remaining, _RETRY_DELAYS_DEAD[0] - 1.0,
            "死窗口要按分钟级退避，不能用 5 秒那一档",
        )

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
    """旧清单能顶多久：45 秒内原样顶住，超过就降级成「只剩最后一片」再顶。"""

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

    def test_stale_beyond_grace_degrades_to_last_segment(self):
        """超过 _STALE_MAX 时降级成「只剩最后一片」的撑住清单，不再抛错。

        为什么要改（实测 2026-11-25）：分片全都超出 CDN 留存之后，继续发一份
        正常清单等于给播放器一个「每一片都取不到」的剧本——它报错、重开、
        又要清单，我们每次都发同一份死清单，于是「卡在同一个片段反复重播」。
        只留最后一片：它播完就安静等，不再触发 onError 重开，上游请求频率也
        就从「跟着播放器重开走」回到我们自己的退避节奏。
        """
        relay, _ = _relay(_playlist(100))
        relay.playlist("cctv1")
        seq_before = relay._windows["cctv1"].first_out
        relay._fetch = _Fetch(500)
        with relay._lock:
            relay._windows["cctv1"].at = time.time() - (_STALE_MAX + 15)
        text, stale, seq = relay.playlist_info("cctv1")
        self.assertTrue(stale, "过期的那份必须标成旧的")
        self.assertEqual(text.count("#EXTINF"), 1, "死窗口只留最后一片，别让播放器逐片 404")
        self.assertNotIn("#EXT-X-START", text, "不能让它再跳回窗口前面重播")
        self.assertGreaterEqual(seq, seq_before, "对外序号不许回退")
        with relay._lock:
            self.assertEqual(
                len(relay._windows["cctv1"].segs), 1,
                "窗口必须真的裁到一片：只改正文的话，上游恢复时序号会回退，播放器卡死",
            )

    def test_dead_window_recovers_with_monotonic_sequence(self):
        """死窗口只留最后一片后，上游恢复时序号必须继续往前，不能回退。

        裁窗口正是为了这个：只改正文不改窗口的话，恢复时 ``first_out`` 会回到
        窗口最前面那个更小的序号，播放器认定「比手里的旧」就卡死在 buffering
        （ExoPlayer #2872）。
        """
        relay, fetch = _relay(_playlist(100))
        relay.playlist("cctv1")
        last_out = relay._windows["cctv1"].out[-1]
        relay._fetch = _Fetch(500)
        with relay._lock:
            relay._windows["cctv1"].at = time.time() - (_STALE_MAX + 15)
        _, _, dead_seq = relay.playlist_info("cctv1")
        self.assertEqual(dead_seq, last_out, "裁到只剩最后一片，序号停在它上面")

        relay._fetch = fetch
        fetch.bodies = [_playlist(140)]           # 上游恢复，而且一下跳了几十片
        text, stale, seq = relay.playlist_info("cctv1")
        self.assertFalse(stale)
        self.assertGreaterEqual(seq, dead_seq, "恢复后对外序号不许回退")
        self.assertIn("seg/140.ts", text)

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


class EntryRotationTest(unittest.TestCase):
    """央视频：一张票两个入口，按 **403** 切，不按固定时间切。

    2026-11-26 实测（NAS 出口）票寿命有 60/110/258 秒三种，写死 55 秒切入口
    是过度拟合。入口什么时候坏只有上游 403 说得准，所以切入口的触发统一收到
    403 那条链（``_rotate_entry`` / ``_fetch_with_entry_failover``）。
    """

    def _relay_with(self, host="live-dtocnc-cdn.ysp.cctv.cn"):
        calls = []

        def resolver(slug):
            calls.append(slug)
            return f"https://{host}/live/index.m3u8?t={len(calls)}", "qqlive"

        return Relay(resolver, warm=False), calls

    def test_same_entry_reused_without_403(self):
        relay, calls = self._relay_with()
        for _ in range(5):
            relay.upstream("cctv1")
        self.assertEqual(len(calls), 1, "没被 403 之前不该换票")
        self.assertEqual(relay.entry_switches, 0, "没被 403 之前不该切入口")

    def test_time_alone_does_not_switch_entry(self):
        """入口 ages 很久也不能仅因时间就切——这是撤掉 _ENTRY_TTL 的关键断言。"""
        relay, calls = self._relay_with()
        relay.upstream("cctv1")
        original = relay._cache["cctv1"][1]
        relay._entries["cctv1"]["at"] -= 3600
        url = relay.upstream("cctv1")[0]
        self.assertEqual(url, original, "过了 1 小时也不该只因时间切走")
        self.assertEqual(relay.entry_switches, 0)
        self.assertEqual(len(calls), 1)

    def test_switches_to_backup_entry_without_new_ticket(self):
        relay, calls = self._relay_with()
        relay.upstream("cctv1")
        self.assertTrue(relay._rotate_entry("cctv1"))
        url = relay.upstream("cctv1")[0]
        self.assertEqual(len(calls), 1, "换入口是同一个 token，不用重新取票")
        self.assertIn("bklive-dtocnc-cdn.ysp.cctv.cn", url, "被拒后必须切备份")
        self.assertEqual(relay.entry_switches, 1)

    def test_retickets_only_after_both_entries_rejected(self):
        relay, calls = self._relay_with()
        relay.upstream("cctv1")          # 主入口
        self.assertTrue(relay._rotate_entry("cctv1"))   # 主被拒 → 备
        relay.upstream("cctv1")
        self.assertFalse(relay._rotate_entry("cctv1"))  # 备也被拒 → 没得切
        relay.upstream("cctv1")          # 两个都被拒 → 重新取票
        self.assertEqual(len(calls), 2)
        self.assertEqual(relay.ticket_rotations, 1)

    def test_ticket_expires_after_total_ttl(self):
        """两个入口都没被拒，但票龄超过上限也要换票。"""
        relay, calls = self._relay_with()
        relay.upstream("cctv1")
        relay._entries["cctv1"]["born"] -= _TICKET_TTL + 1
        relay.upstream("cctv1")
        self.assertEqual(len(calls), 2)

    def test_forbidden_switches_entry_immediately(self):
        relay, calls = self._relay_with()
        relay.upstream("cctv1")
        self.assertTrue(relay._rotate_entry("cctv1"))
        self.assertEqual(len(calls), 1, "被拒时先换入口，不要急着换票")
        self.assertIn("bklive", relay._cache["cctv1"][1])
        self.assertEqual(relay.entry_switches, 1)

    def test_no_entry_left_after_backup_rejected(self):
        relay, _ = self._relay_with()
        relay.upstream("cctv1")
        self.assertTrue(relay._rotate_entry("cctv1"))
        self.assertFalse(relay._rotate_entry("cctv1"), "只有两个入口，备的也拒了就没得切")

    def test_other_hosts_keep_reusing(self):
        """运营商源的一张票能拉很多次，不能跟着央视频的节奏换，那会烧光换票预算。"""
        relay, calls = self._relay_with("cdn.example.com")
        for _ in range(5):
            relay.upstream("cctv1")
        self.assertEqual(len(calls), 1)

    def test_both_entries_forbidden_enters_30s_cooldown(self):
        """主备都 403 = 出口被限流，必须静默 30 秒，而不是每 5 秒再敲一次。

        实测被限后连续 145 秒全是 403；那 145 秒里有一大半是我们自己敲出来的。
        """
        relay, _ = self._relay_with()

        def forbidden(url, ua, *, timeout=None):
            return 403, {}, b"", url

        relay._fetch = forbidden  # type: ignore[method-assign]
        with self.assertRaises(RelayError):
            relay._fetch_raw("cctv1")
        left = relay._next_try["cctv1"] - time.time()
        self.assertGreater(left, 20.0, "主备都被拒之后不该只退避几秒")
        self.assertEqual(relay.entry_switches, 1, "第一次 403 应该先切备份入口")


class LocalDnsFailTest(unittest.TestCase):
    """本机 DNS 失败不能算成「上游拒绝」。

    请求一个字节都没发出去，却让面板说「上游拒绝过 1 次」，会把人引向
    「等解封 / 换 IP」；真正该查的是 NAS 自己的 DNS。
    """

    @staticmethod
    def _boom(slug):  # noqa: ANN001
        raise RuntimeError("获取播放信息失败：域名解析失败")

    def test_counted_separately_from_upstream(self):
        relay, _ = _relay(_playlist(100), resolver=self._boom)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        stats = relay.stats()
        self.assertEqual(stats["failures"], 0, "上游一次都没被请求，不能记成上游拒绝")
        self.assertEqual(stats["dns_failures"], 1)
        self.assertIn("cctv1", stats["dns_errors"])
        self.assertEqual(stats["errors"], {}, "上游错误列表里不该出现本机解析错误")

    def test_does_not_set_cooldown(self):
        relay, _ = _relay(_playlist(100), resolver=self._boom)
        with self.assertRaises(RelayError):
            relay.playlist("cctv1")
        self.assertNotIn(
            "cctv1", relay.stats()["cooling"],
            "DNS 抖动不该把这个台挡几秒——用户点一次黑屏、再点还是同一句错",
        )


class DnsFailCacheTest(unittest.TestCase):
    """失败缓存不能把一次抖动放大成分钟级故障。"""

    def test_fail_cache_stays_short(self):
        from iptvhd import net

        self.assertLessEqual(
            net._DNS_FAIL_TTL, 30.0,
            "失败缓存过长 = 一次 DNS 抖动让这个域名几十秒到两分钟不可用",
        )
        self.assertEqual(net._DNS_TTL, 600.0, "成功缓存不该跟着缩短")


class BridgeSkippedTest(unittest.TestCase):
    """断流恢复后把缺口片接回来，播放器才不会跳一段或重缓冲。

    央视频清单头带 ``EXT-QQHLS-SEGMENT_RANGE:kept-last``，说明 CDN 上还留着
    哪些序号；同一主机同一令牌只换文件名里的序号就能取到。
    """

    HEAD = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:5",
        "#EXT-QQHLS-SEGMENT_RANGE:{}-{}",
    ]

    def _window(self):
        from iptvhd.relay import _Window

        return _Window()

    @staticmethod
    def _segs(first, count=3, host="https://h.example/t"):
        # 地址形如真实清单里的「流 ID-序号.ts」，接片靠的就是文件名里的序号
        return [(5.0, f"{host}/stream-{n}.ts", "", False) for n in range(first, first + count)]

    def test_fills_gap_when_cdn_still_has_it(self):
        from iptvhd.relay import _Window

        window = _Window()
        window.feed([h.format(100, 110) for h in self.HEAD], 99, self._segs(99), 1.0)
        # 断了 2 片（102、103），CDN 从 100 起都还在
        moved = window.feed([h.format(100, 112) for h in self.HEAD], 104, self._segs(104), 2.0)
        self.assertTrue(moved)
        self.assertEqual(len(window.segs), 8, "3 片 + 补回 2 片 + 新 3 片")
        uris = [seg[1] for seg in window.segs]
        self.assertIn("https://h.example/t/stream-102.ts", uris)
        self.assertIn("https://h.example/t/stream-103.ts", uris)
        self.assertEqual([seg[0] for seg in window.segs[3:5]], [5.0, 5.0])
        self.assertFalse(window.segs[3][3], "补回来的片不该带断点标记")

    def test_gives_up_when_gap_is_too_large(self):
        from iptvhd.relay import _BRIDGE_MAX, _Window

        window = _Window()
        window.feed([h.format(100, 110) for h in self.HEAD], 99, self._segs(99), 1.0)
        far = 99 + 3 + _BRIDGE_MAX + 2
        window.feed([h.format(100, 200) for h in self.HEAD], far, self._segs(far), 2.0)
        self.assertEqual(len(window.segs), 3, "缺口太大就重建窗口，不硬补")
        self.assertTrue(window.segs[0][3], "重建后第一片要带断点标记")

    def test_gives_up_when_cdn_already_dropped_it(self):
        from iptvhd.relay import _Window

        window = _Window()
        window.feed([h.format(100, 110) for h in self.HEAD], 99, self._segs(99), 1.0)
        # CDN 只从 103 起还有：缺的 102 已经没了
        window.feed([h.format(103, 112) for h in self.HEAD], 104, self._segs(104), 2.0)
        self.assertEqual(len(window.segs), 3)
        self.assertTrue(window.segs[0][3])

    def test_no_range_tag_means_no_bridging(self):
        """没有 RANGE 标签（运营商、聚合源）就照旧重建，别拿序号瞎拼地址。"""
        from iptvhd.relay import _Window

        window = _Window()
        plain = ["#EXTM3U", "#EXT-X-TARGETDURATION:5"]
        window.feed(plain, 99, self._segs(99), 1.0)
        window.feed(plain, 104, self._segs(104), 2.0)
        self.assertEqual(len(window.segs), 3)


class SegmentSingleFlightTest(unittest.TestCase):
    """同一片被多台设备同时要，只向上游发一次。

    现场症状（2026-11-26 用户实测）：转发模式下两台设备同时播没问题，加第三台
    就开始跳限流。三台设备几乎同时请求同一片，如果各发一次上游请求，出口在
    一瞬间就是 2~3 个并发，CDN 按 IP 判并发/速率直接 403。合并之后后来的请求
    等第一个取完、直接命中缓存。
    """

    class _Slow(Relay):
        def __init__(self):
            super().__init__(
                lambda slug: ("https://live-dtocnc-cdn.ysp.cctv.cn/TOK/i.m3u8", "qqlive"),
                warm=False,
            )
            self.calls = 0

        def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
            self.calls += 1
            time.sleep(0.15)  # 让并发请求真的叠在一起
            return 200, {"Content-Type": "video/mp2t"}, b"x" * 4096, url

    def test_concurrent_requests_hit_upstream_once(self):
        from iptvhd.relay import encode

        relay = self._Slow()
        token = encode("https://live-dtocnc-cdn.ysp.cctv.cn/TOK/2024078603-100.ts?pid=1")
        codes: list[int] = []
        errors: list[str] = []

        def worker():
            try:
                codes.append(relay.resource("cctv1", token)[0])
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(errors, [])
        self.assertEqual(relay.calls, 1, "三台设备要同一片，上游只该被请求一次")
        self.assertEqual(codes, [200, 200, 200])

    def test_cached_segment_skips_upstream(self):
        from iptvhd.relay import encode

        relay = self._Slow()
        token = encode("https://live-dtocnc-cdn.ysp.cctv.cn/TOK/2024078603-200.ts?pid=1")
        relay.resource("cctv1", token)
        relay.resource("cctv1", token)
        self.assertEqual(relay.calls, 1)


class SegmentPrefetchTest(unittest.TestCase):
    """请求第 N 片时，后台把第 N+1 片取好。

    现场（2026-11-26）：三台设备同看一个央视频频道，两分钟被限流。单飞合并只对
    「恰好同一片」有效，而三台播放进度天然错开 1~2 片，实际是三条独立请求流，
    出口速率 ×3。预取让「下一片」提前备好，后来者命中缓存，上游固定 1 次/片。
    """

    class _Relay(Relay):
        def __init__(self, block_next: bool = False):
            super().__init__(
                lambda slug: ("https://live-dtocnc-cdn.ysp.cctv.cn/TOK/i.m3u8", "qqlive"),
                warm=False,
            )
            self.block_next = block_next
            self.seg_calls: list[str] = []

        def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
            if url.endswith(".m3u8"):
                return 200, {}, b"#EXTM3U\n", url
            self.seg_calls.append(url)
            # 只让「下一片」（预取的目标）被拒，模拟出口只对那一枪限流
            if self.block_next and "101" in url:
                return 403, {}, b"", url
            return 200, {"Content-Type": "video/mp2t"}, b"x" * 2048, url

    def _prepared(self, block_next: bool = False):
        from iptvhd.relay import encode

        relay = self._Relay(block_next)
        relay._absorb("cctv1", _playlist(100).decode(), "https://live-dtocnc-cdn.ysp.cctv.cn/TOK/")
        window = relay._windows["cctv1"]
        self.assertGreaterEqual(len(window.segs), 3)
        return relay, encode

    def test_next_segment_is_prefetched(self):
        relay, encode = self._prepared()
        window = relay._windows["cctv1"]
        first, second = window.segs[0][1], window.segs[1][1]
        relay.resource("cctv1", encode(first))
        deadline = time.time() + 3
        while time.time() < deadline and not relay._segments.peek(second):
            time.sleep(0.02)
        self.assertTrue(relay._segments.peek(second), "请求第 1 片后第 2 片应已进缓存")
        self.assertEqual(relay.prefetched, 1)

    def test_second_device_hits_prefetched_segment(self):
        """第二台设备晚一步要同一片 → 命中预取，不再打上游。"""
        relay, encode = self._prepared()
        window = relay._windows["cctv1"]
        first, second = window.segs[0][1], window.segs[1][1]
        relay.resource("cctv1", encode(first))
        deadline = time.time() + 3
        while time.time() < deadline and not relay._segments.peek(second):
            time.sleep(0.02)
        before = len(relay.seg_calls)
        relay.resource("cctv1", encode(second))
        self.assertEqual(len(relay.seg_calls), before, "第二台设备应该直接命中预取")

    def test_does_not_chain_beyond_one_segment(self):
        """预取不沿预取结果继续往前推，否则会取到直播边缘之外。"""
        relay, encode = self._prepared()
        window = relay._windows["cctv1"]
        first, second, third = window.segs[0][1], window.segs[1][1], window.segs[2][1]
        relay.resource("cctv1", encode(first))
        deadline = time.time() + 3
        while time.time() < deadline and not relay._segments.peek(second):
            time.sleep(0.02)
        self.assertTrue(relay._segments.peek(second))
        self.assertFalse(relay._segments.peek(third), "只预取一片，再往后就是浪费")

    def test_forbidden_only_skips_one_segment(self):
        """换入口也救不回时只跳过这一片，不再全局停 30 秒 + 清空队列。

        以前这里一撞 403 就停 30 秒，三台实测预取全程 0 次——等于白开。
        """
        relay, encode = self._prepared(block_next=True)
        window = relay._windows["cctv1"]
        first = window.segs[0][1]
        relay.resource("cctv1", encode(first))
        deadline = time.time() + 3
        while time.time() < deadline and relay.prefetch_failed == 0:
            time.sleep(0.02)
        self.assertGreaterEqual(relay.prefetch_failed, 1)
        self.assertLessEqual(relay._prefetch_pause_until, time.time(), "不该全局暂停预取")


class SegmentEntryFailoverTest(unittest.TestCase):
    """分片 403 时换同一张票的备用入口重取，而不是干等。

    现场（2026-11-27）：三台同看央视频 180 秒出现分片 403 ×2，前台分片
    以前只原址重试一次就抛错——清单侧有换入口救援、分片侧没有，这是缺口。
    """

    MAIN = "https://live-dtocnc-cdn.ysp.cctv.cn/TOK/i.m3u8"
    BACKUP = "https://bklive-dtocnc-cdn.ysp.cctv.cn/TOK/i.m3u8"

    class _Relay(Relay):
        def __init__(self, backup_ok: bool):
            super().__init__(lambda slug: (self.MAIN, "qqlive"), warm=False)
            self.backup_ok = backup_ok
            self.seg_calls: list[str] = []

        def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
            if url.endswith(".m3u8"):
                return 200, {}, b"#EXTM3U\n", url
            self.seg_calls.append(url)
            if "bklive" in url and self.backup_ok:
                return 200, {"Content-Type": "video/mp2t"}, b"y" * 2048, url
            return 403, {}, b"", url

    def _prepared(self, backup_ok: bool):
        from iptvhd.relay import encode

        relay = self._Relay(backup_ok)
        relay._absorb("cctv1", _playlist(100).decode(), "https://live-dtocnc-cdn.ysp.cctv.cn/TOK/")
        now = time.time()
        relay._cache["cctv1"] = (now, self.MAIN, "qqlive")
        relay._entries["cctv1"] = {
            "urls": [self.MAIN, self.BACKUP], "idx": 0, "at": now, "born": now
        }
        return relay, encode

    def test_main_segment_403_falls_back_to_backup(self):
        relay, encode = self._prepared(backup_ok=True)
        window = relay._windows["cctv1"]
        first = window.segs[0][1]
        status, ctype, body, _ = relay.resource("cctv1", encode(first))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"y" * 2048)
        self.assertTrue(any("bklive" in u for u in relay.seg_calls), "应尝试镜像入口")
        self.assertGreaterEqual(relay.segment_entry_switches, 1, "前台分片应被救回")
        self.assertIn("live-dtocnc-cdn", relay._cache["cctv1"][1], "分片救援不该改清单入口")
        self.assertEqual(relay.entry_switches, 0, "分片救援不该触发清单侧入口轮换")

    def test_rescue_never_poisons_entry_state(self):
        """回归：入口已在备用时，连续多片救援不能再把主备都标坏、逼出换票。

        2026-11-27 现场：26 秒内「换到备用入口（第 2/2）」出现 3 次，窗口反复
        重建，播放器卡在同一片。根因是分片救援调了 ``_rotate_entry``：它在
        idx=1（已是最后一片）时会先标坏当前入口，再发现没有下一个而失败。
        """
        relay, encode = self._prepared(backup_ok=True)
        relay._entries["cctv1"]["idx"] = 1
        relay._cache["cctv1"] = (time.time(), self.BACKUP, "qqlive")
        relay._entries["cctv1"]["at"] = time.time() - 200  # 远超旧 _ENTRY_TTL
        segs = relay._windows["cctv1"].segs[:3]
        for seg in segs:
            status, _ctype, _body, _ = relay.resource("cctv1", encode(seg[1]))
            self.assertEqual(status, 200)
        self.assertEqual(relay.entry_switches, 0, "分片救援不该动清单入口索引")
        self.assertEqual(relay._entries["cctv1"]["idx"], 1, "入口索引不该被救援改掉")
        self.assertFalse(relay._entry_bad, "救援不该把任何入口标坏")
        url = relay.upstream("cctv1")[0]
        self.assertEqual(url, self.BACKUP, "清单仍应停在原备用入口，不该被迫换票")

    def test_both_entries_rejected_still_raises(self):
        relay, encode = self._prepared(backup_ok=False)
        first = relay._windows["cctv1"].segs[0][1]
        with self.assertRaises(RelayError):
            relay.resource("cctv1", encode(first))
        self.assertEqual(relay.segment_entry_switches, 0, "备用也拒时不应记成救回")

    def test_non_ysp_segment_keeps_original_retry(self):
        relay, encode = self._prepared(backup_ok=True)
        relay._cache["cctv1"] = (time.time(), "https://cdn.example.com/i.m3u8", "qqlive")
        relay._entries.pop("cctv1", None)
        url = "https://cdn.example.com/seg.ts"
        relay.seg_calls.clear()
        with self.assertRaises(RelayError):
            relay.resource("cctv1", encode(url))
        self.assertTrue(all("bklive" not in u for u in relay.seg_calls), "非央视频不该换入口")


class ShortSegmentWindowTest(unittest.TestCase):
    """CCTV-6 片长 3 秒：清单 3 片只有 9 秒，新鲜期不能按 12 秒算。

    2026-11-27 现场：所有台流畅，唯独 CCTV-6 卡顿、断流、片段重复。实测该台
    恒为 3 秒一片（其他台 4~5 秒），清单同样只给 3 片 = 9 秒；而面板的
    playlist_ttl=10 让 ``_fresh_age`` 变成 12 秒。窗口 9 秒播完后代码仍判定
    「新鲜」，继续下发同一份，``MEDIA-SEQUENCE`` 一动不动，播放器反复播同 3 片。
    """

    def _relay(self, duration: float, ttl: float = 10.0):
        relay = Relay(
            lambda slug: ("https://live-dtocnc-cdn.ysp.cctv.cn/TOK/i.m3u8", "qqlive"),
            warm=False,
            prefetch_every=ttl,
        )
        relay._absorb(
            "cctv6", _playlist(100, duration=duration).decode(), "https://live-dtocnc-cdn.ysp.cctv.cn/TOK/"
        )
        return relay

    def test_short_segments_shrink_freshness(self):
        relay = self._relay(3.0)
        window = relay._windows["cctv6"]
        self.assertAlmostEqual(sum(s[0] for s in window.segs), 9.0)
        self.assertLess(relay._window_ttl(window), relay._fresh_age)
        self.assertLessEqual(relay._window_ttl(window), 7.21)

    def test_normal_segments_keep_fresh_age(self):
        relay = self._relay(5.0)  # 3 片 = 15 秒，够 12 秒的新鲜期
        window = relay._windows["cctv6"]
        self.assertAlmostEqual(relay._window_ttl(window), relay._fresh_age)

    def test_short_window_goes_stale_before_content_runs_out(self):
        """播完 9 秒内容之后不能再当新鲜清单原样下发——这正是重复播放的成因。"""
        relay = self._relay(3.0)
        window = relay._windows["cctv6"]
        window.at -= 8.0
        self.assertGreater(8.0, relay._window_ttl(window), "8 秒时应已判定不新鲜")
        self.assertLess(8.0, relay._fresh_age, "按旧的 12 秒判定会被误认为新鲜")


class Forbidden417Test(unittest.TestCase):
    """417 也是「地址失效」，要换入口；但不算出口被限流，不该整台冷却 30 秒。

    央视频 mobilelive 节点对同一清单地址请求几次就回 417，换 bk 镜像立刻 200
    （2026-11-27 实测）。以前 417 不在 _GONE 里，只做几秒退避、不换入口，只能
    等退避完再撞同一个坏地址——CCTV-6 因此反复断流、横幅反复出现。
    """

    MAIN = "https://mobilelive-cnc-cdn.ysp.cctv.cn/TOK/i.m3u8"
    BACKUP = "https://bkmobilelive-cnc-cdn.ysp.cctv.cn/TOK/i.m3u8"

    def _relay(self, status: int, backup_ok: bool):
        class _R(Relay):
            def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
                if url.endswith(".m3u8"):
                    if "bkmobilelive" in url and backup_ok:
                        return 200, {}, _playlist(100), url
                    return status, {}, b"", url
                return 200, {"Content-Type": "video/mp2t"}, b"x" * 1024, url

        return _R(lambda slug: (self.MAIN, "qqlive"), warm=False)

    def test_417_switches_to_backup_entry(self):
        relay = self._relay(417, backup_ok=True)
        text, _base = relay._fetch_raw("cctv6")
        self.assertIn("#EXTM3U", text)
        self.assertEqual(relay.entry_switches, 1, "417 应触发换入口")
        self.assertEqual(relay._forbidden_until, 0.0, "417 不该触发整台 30 秒冷却")

    def test_417_without_backup_does_not_cool_channel(self):
        relay = self._relay(417, backup_ok=False)
        with self.assertRaises(RelayError):
            relay._fetch_raw("cctv6")
        self.assertEqual(relay._forbidden_until, 0.0, "417 不是出口限流，不该冷却")


class BothEntriesRejectedTest(unittest.TestCase):
    """主备入口都 403 时先换一张新票，而不是直接 30 秒静默。

    2026-11-27 实测 CCTV-6（mobilelive 节点）：主备同时 403 时换新票立刻 200。
    以前这里直接 ``_cool_all_forbidden`` 静默 30 秒，正好把窗口里那 9 秒耗干，
    用户看到的是断流 + 限流横幅反复出现。
    """

    def _relay(self, ticket_ok: bool):
        calls: list[str] = []

        def resolver(slug):  # noqa: ANN001
            calls.append(slug)
            return f"https://live-dtocnc-cdn.ysp.cctv.cn/T{len(calls)}/i.m3u8", "qqlive"

        class _R(Relay):
            def _fetch(self, url, ua, *, timeout=None):  # noqa: ANN001 - 测试替身
                if url.endswith(".m3u8"):
                    if "bklive" in url:
                        return 403, {}, b"", url
                    if ticket_ok and "/T2/" in url:
                        return 200, {}, _playlist(100), url
                    return 403, {}, b"", url
                return 200, {"Content-Type": "video/mp2t"}, b"x", url

        return _R(resolver, warm=False), calls

    def test_new_ticket_rescues_both_entries_rejected(self):
        relay, calls = self._relay(ticket_ok=True)
        text, _base = relay._fetch_raw("cctv6")
        self.assertIn("#EXTM3U", text)
        self.assertEqual(len(calls), 2, "主备都拒后应换一张新票")
        self.assertEqual(relay._forbidden_until, 0.0, "换票救回就不该整台冷却 30 秒")

    def test_still_rejected_after_new_ticket_cools(self):
        relay, calls = self._relay(ticket_ok=False)
        with self.assertRaises(RelayError):
            relay._fetch_raw("cctv6")
        self.assertEqual(len(calls), 2, "只换一次票，不要反复抽")
        self.assertGreater(relay._forbidden_until, time.time(), "换票仍被拒才认定出口限流")


if __name__ == "__main__":
    unittest.main()
