"""低频抽测逻辑单测：不联网。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iptvhd import pipeline
from iptvhd.models import Stream
from iptvhd.settings import Settings


def _settings(root: Path) -> Settings:
    return Settings(root=root)


class TestMeasureRound(unittest.TestCase):
    def test_enable_measure_only_touches_yangshipin(self):
        sources = [
            {"type": "yangshipin", "name": "主源", "resolve": False, "verify_segment": False},
            {"type": "subscription", "name": "订阅", "url": "http://x/a.m3u"},
        ]
        out = pipeline._enable_measure(sources)
        self.assertTrue(out[0]["resolve"])
        self.assertTrue(out[0]["verify_segment"])
        # 非主源不动
        self.assertNotIn("resolve", out[1])
        # 原列表不被污染
        self.assertFalse(sources[0]["resolve"])

    def test_apply_measure_cache_backfills_by_slug(self):
        root = Path(tempfile.mkdtemp())
        settings = _settings(root)
        (settings.data_dir).mkdir(parents=True, exist_ok=True)
        pipeline._save_measure(settings, {
            "cctv1": {"bandwidth": 6000000, "speed_mbps": 12.3, "delay_ms": 80, "at": "2026-11-07 10:00:00"},
        })
        s = Stream(url="", slug="cctv1", raw_name="CCTV-1")
        pipeline._apply_measure_cache([s], settings)
        self.assertEqual(s.bandwidth, 6000000)
        self.assertEqual(s.speed_mbps, 12.3)
        self.assertEqual(s.delay_ms, 80)
        self.assertEqual(s.note, "历史抽测")

    def test_apply_measure_cache_keeps_existing_values(self):
        root = Path(tempfile.mkdtemp())
        settings = _settings(root)
        (settings.data_dir).mkdir(parents=True, exist_ok=True)
        pipeline._save_measure(settings, {
            "cctv1": {"bandwidth": 6000000, "speed_mbps": 12.3, "delay_ms": 80},
        })
        # 已有真实测速值的流不应被历史值覆盖
        s = Stream(url="", slug="cctv1", raw_name="CCTV-1", bandwidth=8000000, speed_mbps=15.0)
        pipeline._apply_measure_cache([s], settings)
        self.assertEqual(s.bandwidth, 8000000)
        self.assertEqual(s.speed_mbps, 15.0)

    def test_extract_measure_roundtrip(self):
        root = Path(tempfile.mkdtemp())
        settings = _settings(root)
        (settings.data_dir).mkdir(parents=True, exist_ok=True)
        s = Stream(url="", slug="hnws", raw_name="湖南卫视", bandwidth=5000000, speed_mbps=9.1, delay_ms=60)
        pipeline._extract_measure([s], settings)
        cache = pipeline._load_measure(settings)
        self.assertIn("hnws", cache)
        self.assertEqual(cache["hnws"]["bandwidth"], 5000000)

    def test_measure_round_cycles(self):
        root = Path(tempfile.mkdtemp())
        settings = _settings(root)
        (settings.data_dir).mkdir(parents=True, exist_ok=True)
        counter = settings.data_dir / "measure_round"
        # every=4：第 4 轮抽测，其余不抽
        results = []
        for _ in range(4):
            results.append(pipeline._is_measure_round(settings))
        self.assertEqual(results, [False, False, False, True])
        self.assertEqual(counter.read_text(encoding="utf-8"), "4")


if __name__ == "__main__":
    unittest.main()
