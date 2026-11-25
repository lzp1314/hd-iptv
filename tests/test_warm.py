"""换台预热（P1）：只预热「下一个央视频台」，而且必须自我克制。

背景：换到一个没人看过的台，第一份清单要现换票再取，两个串行往返约 0.3~0.6 秒，
叠加播放器取首片、初始化解码器，就是用户说的「换台要等 1 秒」。

这里锁的不是「预热能用」，而是**它不会把限流做大**：

* 连点换台（每个台只碰一次）永远进不了「在播」判定，所以不预热；
* 出口余量不足就不预热，不能为下一个台挤掉在播台的刷新；
* 预热失败不留痕：不记 failures、不设退避——否则一次预热失败会把目标台标成
  「刚被拒」，用户切过去反而被挡在门外，比不预热更糟。
"""

from __future__ import annotations

import time
import unittest

from iptvhd.models import Channel, Stream
from iptvhd.relay import (
    Relay,
    _ACTIVE_FOR,
    _WARM_DELAY,
    _WARM_MIN_GAP,
    _WARM_MIN_TOKENS,
)
from iptvhd.service import warm_neighbors


def _playlist(seq: int) -> bytes:
    head = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        f"#EXT-QQHLS-SEGMENT_RANGE:{seq - 8}-{seq + 2}\n"
        f"#EXT-X-MEDIA-SEQUENCE:{seq}\n"
    )
    body = "".join(f"#EXTINF:5.0,\nseg/{seq + i}.ts\n" for i in range(3))
    return (head + body).encode("utf-8")


class _Fetch:
    def __init__(self, body):
        self.body = body
        self.calls = 0

    def __call__(self, url, ua, timeout=None):  # noqa: ANN001
        self.calls += 1
        if isinstance(self.body, int):
            return self.body, {}, b"", url
        return 200, {}, self.body, url


def _relay(body=_playlist(100)):
    relay = Relay(lambda slug: ("https://cdn.example.com/live/index.m3u8", "qqlive"), warm=False)
    fetch = _Fetch(body)
    relay._fetch = fetch
    return relay, fetch


class WarmFetchTest(unittest.TestCase):
    def test_warm_fills_window(self):
        relay, fetch = _relay()
        self.assertTrue(relay.warm("cctv2"))
        self.assertEqual(fetch.calls, 1)
        self.assertTrue(relay._windows["cctv2"].segs, "预热就该把窗口攒起来，换过去才能秒出")
        self.assertEqual(relay.stats()["warm_runs"], 1)

    def test_warm_skips_when_already_fresh(self):
        """同一个台预热完不该马上再取——20 秒单台间隔。"""
        relay, fetch = _relay()
        self.assertTrue(relay.warm("cctv2"))
        self.assertFalse(relay.warm("cctv2"))
        self.assertEqual(fetch.calls, 1)

    def test_warm_backs_off_after_attempt(self):
        """失败也算一次尝试：不在这 20 秒里反复撞。"""
        relay, fetch = _relay(body=403)
        self.assertFalse(relay.warm("cctv2"))
        self.assertFalse(relay.warm("cctv2"))
        self.assertEqual(fetch.calls, 1)
        self.assertGreaterEqual(_WARM_MIN_GAP, 1.0)

    def test_warm_skips_when_budget_low(self):
        """出口只剩几格时，配额留给在播的台。"""
        relay, fetch = _relay()
        relay._tokens = _WARM_MIN_TOKENS - 1
        relay._tokens_at = time.monotonic()
        self.assertFalse(relay.warm("cctv2"))
        self.assertEqual(fetch.calls, 0)

    def test_warm_skips_channel_in_backoff(self):
        relay, fetch = _relay()
        relay._next_try["cctv2"] = time.time() + 10
        self.assertFalse(relay.warm("cctv2"))
        self.assertEqual(fetch.calls, 0)

    def test_warm_failure_leaves_no_trace(self):
        """预热失败不能污染统计，更不能给目标台设退避。

        否则用户切过去正好撞上 ``_next_try``，看到的是「本台线路暂时受限」——
        预热反而制造了换台失败。
        """
        relay, _ = _relay(body=403)
        self.assertFalse(relay.warm("cctv2"))
        self.assertEqual(relay.failures, 0, "预热失败不进 failures")
        self.assertEqual(relay._next_try, {}, "预热失败不设退避")
        self.assertEqual(relay._errors, {}, "预热失败不记错误")
        self.assertEqual(len(relay._events), 0, "预热失败不写运行日志")
        self.assertEqual(relay.stats()["warm_runs"], 0)

    def test_warm_403_does_not_retry(self):
        """soft 只打一枪：预热的目的是省一次换台等待，多睡 1 秒重试反而更慢。"""
        relay, fetch = _relay(body=403)
        relay.warm("cctv2")
        self.assertEqual(fetch.calls, 1, "预热被拒不该重试")


class WarmPlanTest(unittest.TestCase):
    def test_plan_only_when_entering_playback(self):
        relay, _ = _relay()
        relay.set_neighbor_map({"cctv1": "cctv2"})
        relay._hits["cctv1"] = time.time() - 5          # 间隔落在「在播」区间
        self.assertTrue(relay._note_hit("cctv1"))
        self.assertIn("cctv2", relay._warm_due)
        due, source = relay._warm_due["cctv2"]
        self.assertEqual(source, "cctv1")
        self.assertGreaterEqual(due - time.time(), _WARM_DELAY - 1)

        # 已经在播的台再被请求，不再重复排
        relay._hits["cctv1"] = time.time() - 5
        relay._note_hit("cctv1")
        self.assertEqual(len(relay._warm_due), 1)

    def test_scan_like_touches_never_plan(self):
        """扫台（每台只碰一次、或毫秒级连打）不算在看，不预热。"""
        relay, _ = _relay()
        relay.set_neighbor_map({"cctv1": "cctv2", "cctv2": "cctv3"})
        for slug in ("cctv1", "cctv2", "cctv3"):
            relay._note_hit(slug)
        self.assertEqual(relay._warm_due, {})

    def test_no_neighbor_no_plan(self):
        relay, _ = _relay()
        relay.set_neighbor_map({})
        relay._hits["cctv9"] = time.time() - 5
        relay._note_hit("cctv9")
        self.assertEqual(relay._warm_due, {})

    def test_sweep_fires_after_delay(self):
        relay, _ = _relay()
        relay._active["cctv1"] = time.time()
        relay._warm_due["cctv2"] = (time.time() - 1, "cctv1")
        calls = []
        relay.warm_async = calls.append
        relay._sweep()
        self.assertEqual(calls, ["cctv2"])

    def test_sweep_drops_when_source_gone(self):
        """触发台已经没人看了，就不花这次配额。"""
        relay, _ = _relay()
        relay._active["cctv1"] = time.time() - (_ACTIVE_FOR + 5)
        relay._warm_due["cctv2"] = (time.time() - 1, "cctv1")
        calls = []
        relay.warm_async = calls.append
        relay._sweep()
        self.assertEqual(calls, [])
        self.assertEqual(relay._warm_due, {})

    def test_sweep_waits_until_due(self):
        relay, _ = _relay()
        relay._active["cctv1"] = time.time()
        relay._warm_due["cctv2"] = (time.time() + 60, "cctv1")
        calls = []
        relay.warm_async = calls.append
        relay._sweep()
        self.assertEqual(calls, [])


class NeighborMapTest(unittest.TestCase):
    def test_only_adjacent_ysp_channels(self):
        channels = [
            Channel(name="CCTV-1 综合", streams=[Stream(url="", slug="cctv1")]),
            Channel(name="CCTV-2 财经", streams=[Stream(url="", slug="cctv2")]),
            Channel(name="湖南卫视", streams=[Stream(url="", slug="hnws")]),
            Channel(name="某聚合台", streams=[Stream(url="http://x/a.m3u8")]),
            Channel(name="CCTV-3 综艺", streams=[Stream(url="", slug="cctv3")]),
        ]
        self.assertEqual(warm_neighbors(channels), {"cctv1": "cctv2", "cctv2": "hnws"})

    def test_empty_and_single(self):
        self.assertEqual(warm_neighbors([]), {})
        one = [Channel(name="CCTV-1 综合", streams=[Stream(url="", slug="cctv1")])]
        self.assertEqual(warm_neighbors(one), {})


if __name__ == "__main__":
    unittest.main()
