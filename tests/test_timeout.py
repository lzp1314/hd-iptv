"""探测时限测试。

这里守的是一条具体的故障线：源「连得上、但一个字节一个字节往回挤」时，
socket 超时（多久没数据）永远不会触发，整轮更新会无限期挂在
``[探测] 297/332`` 上。两道闸门都要能兜住：

* 单条流的绝对时限（``HttpClient(deadline=...)`` / ``probe_one``）
* 整批探测的总预算（``probe_many(budget=...)``）
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from iptvhd.models import Stream
from iptvhd.net import HttpClient, Response, resolve
from iptvhd.probe import _Slow, _fetch, probe_many, probe_one


class Trickle:
    """每次 read 只挤一个字节，模拟慢滴流。"""

    def __init__(self, per_read: float = 0.05):
        self.per_read = per_read
        self.reads = 0

    def read(self, _amount: int) -> bytes:
        self.reads += 1
        time.sleep(self.per_read)
        return b"\0"


class DeadlineTest(unittest.TestCase):
    def test_read_body_stops_at_deadline(self):
        raw = Trickle(per_read=0.02)
        started = time.monotonic()
        body, truncated = HttpClient._read_body(raw, 262144, started + 0.3)
        elapsed = time.monotonic() - started
        self.assertTrue(truncated, "慢滴流必须被判为截断")
        self.assertLess(elapsed, 2.0, "到点就要走，不能陪着读完 256KB")
        self.assertLess(len(body), 262144)

    def test_read_body_without_deadline_reads_all(self):
        class Short:
            def __init__(self):
                self.buf = [b"abc", b"de", b""]

            def read(self, _amount):
                return self.buf.pop(0)

        body, truncated = HttpClient._read_body(Short(), 0, None)
        self.assertEqual(body, b"abcde")
        self.assertFalse(truncated)

    def test_fetch_raises_on_truncated(self):
        client = HttpClient()
        with mock.patch.object(client, "get", return_value=Response(status=200, body=b"x", truncated=True)):
            with self.assertRaises(_Slow):
                _fetch(client, "http://example.com/a.m3u8", limit=1024)

    def test_slow_source_is_dropped(self):
        stream = Stream(url="http://example.com/live.m3u8", channel="CCTV-1")
        client = HttpClient()
        with mock.patch.object(
            client, "get", return_value=Response(status=200, body=b"#EXTM3U\n", truncated=True)
        ):
            out = probe_one(stream, client, 262144, time.monotonic() + 5)
        self.assertFalse(out.ok)
        self.assertIn("超时", out.error)


class BudgetTest(unittest.TestCase):
    def _streams(self, n: int) -> list[Stream]:
        return [Stream(url=f"http://example.com/{i}.m3u8", channel=f"CH{i}") for i in range(n)]

    def test_budget_cuts_long_tail(self):
        streams = self._streams(4)
        progress: list[tuple[int, int]] = []
        stalls: list[tuple[int, float]] = []

        def slow_probe(stream, _client, _sample, deadline=None):  # noqa: ANN001 - 测试替身
            time.sleep(5)
            stream.ok = True
            return stream

        started = time.monotonic()
        with mock.patch("iptvhd.probe.probe_one", side_effect=slow_probe):
            probe_many(
                streams,
                HttpClient(),
                concurrency=2,
                budget=0.4,
                per_stream_timeout=5,
                on_progress=lambda done, total: progress.append((done, total)),
                on_stall=lambda pending, secs: stalls.append((pending, secs)),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0, "预算到点必须收工，不能等 5 秒的慢探测")
        self.assertTrue(all(not s.ok for s in streams))
        self.assertTrue(all("超时" in s.error for s in streams))

    def test_progress_and_order_are_kept(self):
        streams = self._streams(3)
        seen: list[int] = []

        def fast_probe(stream, _client, _sample, deadline=None):  # noqa: ANN001 - 测试替身
            stream.ok = True
            return stream

        with mock.patch("iptvhd.probe.probe_one", side_effect=fast_probe):
            out = probe_many(
                streams,
                HttpClient(),
                concurrency=2,
                on_progress=lambda done, total: seen.append(done),
            )

        self.assertEqual([s.channel for s in out], ["CH0", "CH1", "CH2"], "返回顺序要与输入一致")
        self.assertEqual(seen[-1], 3)
        self.assertTrue(all(s.ok for s in out))

    def test_timeout_starts_when_probe_starts(self):
        """时限要从「真正开始探测」算，不能从「提交任务」算。

        回归用例：332 条排 14 批，如果截止时刻在提交时就算死，排在后面的
        好源一启动就已经超时，会被成片误杀。
        """
        streams = self._streams(6)
        got: list[float | None] = []

        def probe_with_own_deadline(stream, client, sample, deadline=None):  # noqa: ANN001
            got.append(deadline)
            time.sleep(0.35)
            stream.ok = True
            return stream

        started = time.monotonic()
        with mock.patch("iptvhd.probe.probe_one", side_effect=probe_with_own_deadline):
            out = probe_many(streams, HttpClient(), concurrency=1, per_stream_timeout=0.6, budget=30)
        elapsed = time.monotonic() - started

        self.assertTrue(all(s.ok for s in out), "排队等待的时间不该算进单条时限")
        self.assertGreater(elapsed, 1.5, "串行跑完 6 条必然超过单条时限，正好用来验证时限是逐条重算的")
        self.assertTrue(all(d is not None for d in got))
        # 每条都是「自己开始时 + 0.6」，所以越晚开始的截止时刻越晚
        self.assertEqual(got, sorted(got))

    def test_empty_input(self):
        self.assertEqual(probe_many([], HttpClient()), [])


class _Hang:
    """模拟解析器卡死：getaddrinfo 迟迟不返回。"""

    def __init__(self, delay: float = 3.0):
        self.delay = delay
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        time.sleep(self.delay)
        return [(2, 1, 6, "", ("127.0.0.1", 80))]


class ResolveTest(unittest.TestCase):
    """getaddrinfo 不受 socket 超时约束，必须单独设闸。"""

    def test_hanging_resolver_times_out(self):
        hang = _Hang(3.0)
        started = time.monotonic()
        with mock.patch("iptvhd.net.socket.getaddrinfo", hang):
            state = resolve("hang-test.example", 80, started + 0.3)
        self.assertIsNone(state, "解析卡住时要报超时，而不是一直等")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_success_is_cached(self):
        calls = []

        def fake(host, port, *_a, **_k):
            calls.append(host)
            return [(2, 1, 6, "", ("127.0.0.1", port))]

        with mock.patch("iptvhd.net.socket.getaddrinfo", fake):
            self.assertTrue(resolve("cache-test.example", 80))
            self.assertTrue(resolve("cache-test.example", 80))
        self.assertEqual(len(calls), 1, "同一个域名不该反复解析")

    def test_dns_timeout_marks_response_truncated(self):
        client = HttpClient()
        started = time.monotonic()
        with mock.patch("iptvhd.net.socket.getaddrinfo", _Hang(3.0)):
            resp = client.get("http://dns-timeout-test.example/a.m3u8", limit=1024, deadline=started + 0.3)
        self.assertTrue(resp.truncated)
        self.assertIn("DNS", resp.error)


class DeadlinePruneTest(unittest.TestCase):
    """线程卡在 socket 之外的调用上时，调度线程要主动放手。"""

    def test_stuck_thread_does_not_block_batch(self):
        streams = [Stream(url=f"http://example.com/{i}.m3u8", channel=f"CH{i}") for i in range(3)]

        def stuck(stream, _client, _sample, deadline=None):  # noqa: ANN001 - 无视时限的替身
            time.sleep(2.0)
            stream.ok = True
            return stream

        started = time.monotonic()
        with mock.patch("iptvhd.probe.probe_one", side_effect=stuck):
            probe_many(
                streams,
                HttpClient(),
                concurrency=3,
                per_stream_timeout=0.3,
                deadline_grace=0.2,
                budget=30,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5, "到点就该放手，不能陪着卡住的线程等 2 秒")
        self.assertTrue(all(not s.ok for s in streams))
        self.assertTrue(all(s.error == "探测超时" for s in streams))


class ProbeStatsTest(unittest.TestCase):
    """成功分支必须记下速率与分辨率。

    回归用例：这四行曾写在「分片不可用」的分支里，只在失败时执行。结果是
    所有能播的流 speed=0、height=0，再被「画质 ≥720P」整批筛掉——卫视直连
    源几乎全军覆没，只能落回央视频兜底（播放时每 15 秒换一次票）。
    """

    def test_successful_probe_records_speed_and_bitrate(self):
        manifest = b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nseg1.ts\n#EXTINF:5,\nseg2.ts\n"

        class Resp:
            def __init__(self, status, body, ctype=""):
                self.status = status
                self.body = body
                self.url = "http://cdn/live.m3u8"
                self.headers = {"content-length": str(len(body)), "content-type": ctype}
                self.ttfb_ms = 20
                self.elapsed_ms = 100
                self.error = ""
                self.truncated = False

            @property
            def text(self):
                return self.body.decode("utf-8", "ignore")

            @property
            def speed_mbps(self):
                return 8.0 if self.body else 0.0

        class Fake:
            def get(self, url, **k):  # noqa: ANN003
                if url.endswith(".m3u8"):
                    return Resp(200, manifest)
                return Resp(206, b"\x00" * 20000)

        stream = Stream(url="http://cdn/live.m3u8", channel="湖南卫视", hint_height=1080)
        out = probe_one(stream, Fake(), 262144, time.monotonic() + 5)

        self.assertTrue(out.ok)
        self.assertGreater(out.speed_mbps, 0, "能播的流必须留下实测速率")
        self.assertGreater(out.bandwidth, 0, "分片大小 + 时长要能算出码率")
        self.assertEqual(out.height, 1080, "读不到分辨率时用采集端已知档位")


if __name__ == "__main__":
    unittest.main()
