"""线路历史成绩：记录、回填、是否真的改变了排序。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from iptvhd import history
from iptvhd.models import HIST_MIN_SAMPLES, Stream
from iptvhd.settings import Settings


def _settings(root: Path | None = None) -> Settings:
    return Settings(root=root or Path(tempfile.mkdtemp()))


def _stream(url: str = "http://a.example/live.m3u8", **kw) -> Stream:
    return Stream(url=url, raw_name="CCTV-1", **kw)


class TestRecordAndApply(unittest.TestCase):
    def test_counts_ok_and_fail(self):
        s = _settings()
        book = history.load(s)
        for ok in (True, True, False):
            stream = _stream()
            stream.ok = ok
            history.record(s, [stream], book)

        fresh = _stream()
        history.apply([fresh], history.load(s))
        self.assertEqual(fresh.hist_samples, 3)
        self.assertAlmostEqual(fresh.hist_rate, 2 / 3, places=3)
        self.assertEqual(fresh.hist_fails, 1)

    def test_consecutive_fails_reset_on_success(self):
        s = _settings()
        book = history.load(s)
        for ok in (False, False, True):
            stream = _stream()
            stream.ok = ok
            history.record(s, [stream], book)
        fresh = _stream()
        history.apply([fresh], book)
        self.assertEqual(fresh.hist_fails, 0)

    def test_file_survives_restart(self):
        s = _settings()
        stream = _stream()
        stream.ok = True
        history.record(s, [stream])
        self.assertTrue((s.data_dir / "history.json").exists())
        again = _stream()
        history.apply([again], history.load(s))
        self.assertEqual(again.hist_samples, 1)

    def test_trusted_not_recorded(self):
        s = _settings()
        stream = _stream("http://[2409:8087::1]/x.m3u8", trusted=True)
        stream.ok = False
        book = history.record(s, [stream], history.load(s))
        self.assertEqual(book["lines"], {})

    def test_slug_is_stable_key(self):
        # 央视频那类源每轮地址都换，但 slug 不变，历史要跟着 slug 走
        a = _stream("http://a.example/1.m3u8", slug="cctv1")
        b = _stream("http://b.example/2.m3u8", slug="cctv1")
        self.assertEqual(history.stream_key(a), history.stream_key(b))
        c = _stream("http://c.example/3.m3u8", slug="cctv2")
        self.assertNotEqual(history.stream_key(a), history.stream_key(c))

    def test_broken_file_is_empty(self):
        s = _settings()
        s.data_dir.mkdir(parents=True, exist_ok=True)
        (s.data_dir / "history.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(history.load(s)["lines"], {})

    def test_entry_cap(self):
        s = _settings()
        lines = {f"k{i}": {"ok": 1, "all": 1, "fails": 0, "at": i} for i in range(history.MAX_ENTRIES + 50)}
        book = history.record(s, [], {"lines": lines})
        self.assertEqual(len(book["lines"]), history.MAX_ENTRIES)
        # 淘汰的是最旧的那批
        self.assertNotIn("k0", book["lines"])
        self.assertIn(f"k{history.MAX_ENTRIES + 49}", book["lines"])

    def test_no_double_count_within_one_round(self):
        s = _settings()
        a = _stream()
        b = _stream()  # 同一个地址的两条重复条目
        a.ok = b.ok = True
        history.record(s, [a, b])
        fresh = _stream()
        history.apply([fresh], history.load(s))
        self.assertEqual(fresh.hist_samples, 1)


class TestBucket(unittest.TestCase):
    def test_unknown_when_samples_missing(self):
        s = _stream()
        self.assertEqual(s.hist_bucket(), 1)
        s.hist_samples = HIST_MIN_SAMPLES - 1
        s.hist_rate = 0.0
        self.assertEqual(s.hist_bucket(), 1)

    def test_stable_and_flaky(self):
        s = _stream()
        s.hist_samples = 10
        s.hist_rate = 0.9
        self.assertEqual(s.hist_bucket(), 0)
        s.hist_rate = 0.3
        self.assertEqual(s.hist_bucket(), 2)
        s.hist_rate = 0.6
        self.assertEqual(s.hist_bucket(), 1)


class TestOrdering(unittest.TestCase):
    def test_flaky_hd_loses_to_stable_sd(self):
        """历史上老掉线的 1080P，不能抢走 720P 稳定线路的位置。

        列表每个台只写一行，排第一的那条就是用户真正会播的那条。
        """
        flaky = _stream("http://flaky.example/x.m3u8", height=1080)
        flaky.hist_samples = 10
        flaky.hist_rate = 0.2

        stable = _stream("http://stable.example/x.m3u8", height=720)
        stable.hist_samples = 10
        stable.hist_rate = 0.95

        self.assertLess(stable.sort_key(), flaky.sort_key())

    def test_unknown_history_keeps_quality_first(self):
        """没样本的源不吃亏：同档条件下，画质高的仍然排前面。"""
        hd = _stream("http://hd.example/x.m3u8", height=1080)
        sd = _stream("http://sd.example/x.m3u8", height=720)
        self.assertLess(hd.sort_key(), sd.sort_key())

    def test_stable_beats_unknown_same_height(self):
        stable = _stream("http://a.example/x.m3u8", height=1080)
        stable.hist_samples = 8
        stable.hist_rate = 0.9
        unknown = _stream("http://b.example/x.m3u8", height=1080)
        self.assertLess(stable.sort_key(), unknown.sort_key())


class TestPipelineWiring(unittest.TestCase):
    def test_round_uses_old_book_then_records(self):
        """排序用的是**上一轮**的成绩，本轮结果只记账不参与本次排序。"""
        s = _settings()
        # 先造两轮历史：一条很稳，一条一直失败
        for _ in range(3):
            good = _stream("http://good.example/x.m3u8", height=720)
            good.ok = True
            bad = _stream("http://bad.example/x.m3u8", height=1080)
            bad.ok = False
            history.record(s, [good, bad])

        book = history.load(s)
        fresh_good = _stream("http://good.example/x.m3u8", height=720)
        fresh_bad = _stream("http://bad.example/x.m3u8", height=1080)
        history.apply([fresh_good, fresh_bad], book)

        self.assertEqual(fresh_good.hist_bucket(), 0)
        self.assertEqual(fresh_bad.hist_bucket(), 2)
        self.assertLess(fresh_good.sort_key(), fresh_bad.sort_key())

        # 本轮结果记进账本后，样本数 +1
        fresh_good.ok = True
        fresh_bad.ok = True
        data = history.record(s, [fresh_good, fresh_bad], book)
        row = data["lines"][history.stream_key(fresh_good)]
        self.assertEqual(row["all"], 4)


if __name__ == "__main__":
    unittest.main()
