"""单模块增量更新：只刷新目标模块，其它模块的频道与统计原样保留。

要点：
- _rows_to_channels 把落盘结果完整还原（含 grade/source 等写盘字段）；
- 合并时丢掉目标模块旧条目、换成新采到的，别的模块一个字段都不动；
- 目标模块本轮空结果时整组保留旧的，绝不清空；
- report 统计把本轮数并回上一轮，面板全局数字不归零。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from iptvhd.models import Channel, RunReport, Stream
from iptvhd.pipeline import _merge_module_report, _merge_module_result, _rows_to_channels
from iptvhd.settings import load_settings


def _channel(name: str, group: str, url: str, *, source: str = "", height: int = 720) -> Channel:
    s = Stream(url=url, source=source, height=height, bandwidth=3_000_000, ok=True)
    return Channel(name=name, group=group, streams=[s])


def _project_with_result(channels: list[Channel], report: dict) -> tuple[Path, object]:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config").mkdir()
    (tmp / "config" / "sources.toml").write_text(
        '[[source]]\nname = "运营商官方源"\ntype = "local"\npath = "a.txt"\nenabled = true\n'
        '[[source]]\nname = "咪咕视频"\ntype = "local"\npath = "b.txt"\nenabled = true\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp)
    payload = {
        "generated_at": "2026-01-01 00:00:00",
        "report": report,
        "channels": [
            {
                "name": ch.name,
                "group": ch.group,
                "streams": [
                    {
                        "url": s.url,
                        "slug": s.slug,
                        "tvg_id": s.tvg_id,
                        "height": s.height,
                        "width": s.width,
                        "grade": s.grade,
                        "codec": s.codec,
                        "bandwidth_kbps": round(s.bandwidth / 1000),
                        "speed_mbps": s.speed_mbps,
                        "source": s.source,
                    }
                    for s in ch.streams
                ],
            }
            for ch in channels
        ],
    }
    (tmp / "output").mkdir()
    (tmp / "output" / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp, settings


class TestIncrementalMerge(unittest.TestCase):
    def test_rows_to_channels_keeps_written_fields(self):
        rows = [
            {
                "name": "央视一套",
                "group": "咪咕视频",
                "streams": [{
                    "url": "http://x/a.m3u8", "slug": "m:100", "height": 1080,
                    "codec": "h264", "bandwidth_kbps": 6000, "source": "咪咕视频",
                    "speed_mbps": 5.5, "delay_ms": 120, "note": "旧",
                }],
            }
        ]
        got = _rows_to_channels(rows)
        self.assertEqual(len(got), 1)
        s = got[0].streams[0]
        self.assertEqual(s.slug, "m:100")
        self.assertEqual(s.grade, "1080P")
        self.assertEqual(s.bandwidth, 6_000_000)
        self.assertEqual(s.source, "咪咕视频")
        self.assertEqual(s.note, "旧")

    def test_merge_replaces_only_target_module(self):
        old = [
            _channel("央视一套", "咪咕视频", "http://old/cctv1.m3u8", source="咪咕视频"),
            _channel("湖北卫视", "运营商官方源", "http://old/hubei.m3u8", source="官方直连"),
        ]
        prev_report = {
            "collected": 100, "probed": 80, "alive": 40,
            "channels_total": 20, "channels_matched": 18,
            "sources": [
                {"name": "咪咕视频", "count": 10},
                {"name": "官方直连", "count": 12},
            ],
            "missing": [{"name": "某台"}],
        }
        root, settings = _project_with_result(old, prev_report)

        fresh = [_channel("央视一套", "咪咕视频", "http://new/cctv1.m3u8", source="咪咕视频")]
        merged, prev = _merge_module_result(settings, fresh, "咪咕视频", lambda _m: None)
        by_key = {(c.group, c.name): c for c in merged}
        # 目标模块换成新的
        self.assertEqual(by_key[("咪咕视频", "央视一套")].streams[0].url, "http://new/cctv1.m3u8")
        # 其它模块原样保留
        self.assertEqual(by_key[("运营商官方源", "湖北卫视")].streams[0].url, "http://old/hubei.m3u8")
        self.assertEqual(len(merged), 2)

        report = RunReport(collected=8, probed=6, alive=5, channels_total=2, channels_matched=2,
                           sources=[{"name": "咪咕视频", "count": 8}], missing=[])
        _merge_module_report(report, prev, "咪咕视频")
        self.assertEqual(report.scope_module, "咪咕视频")
        self.assertEqual(report.collected, 108)
        self.assertEqual(report.channels_total, 22)
        names = [s["name"] for s in report.sources]
        self.assertEqual(names, ["官方直连", "咪咕视频"])
        self.assertEqual(len(report.missing), 1)

    def test_empty_fresh_keeps_old_module_channels(self):
        old = [
            _channel("央视一套", "咪咕视频", "http://old/cctv1.m3u8"),
            _channel("湖北卫视", "运营商官方源", "http://old/hubei.m3u8"),
        ]
        root, settings = _project_with_result(old, {})
        merged, _ = _merge_module_result(settings, [], "咪咕视频", lambda _m: None)
        by_key = {(c.group, c.name): c for c in merged}
        self.assertEqual(by_key[("咪咕视频", "央视一套")].streams[0].url, "http://old/cctv1.m3u8")
        self.assertEqual(len(merged), 2)

    def test_builtin_module_members_group_are_preserved(self):
        """运营商官方源的底层源名各异，但 group 都是展示名，合并按展示名比对。"""
        old = [_channel("央视一套", "运营商官方源", "http://old/cctv1.m3u8", source="官方直连")]
        root, settings = _project_with_result(old, {})
        fresh = [_channel("央视一套", "运营商官方源", "http://new/cctv1.m3u8", source="本省自动源")]
        merged, _ = _merge_module_result(settings, fresh, "运营商官方源", lambda _m: None)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].streams[0].url, "http://new/cctv1.m3u8")


if __name__ == "__main__":
    unittest.main()
