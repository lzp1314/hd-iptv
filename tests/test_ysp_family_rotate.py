"""央视频换票的地址族轮换：清单 403 时必须换一条链路重换票。

背景（2026-11-25 实测）：play_info 按换票时的出口地址族分配 CDN 节点。
用 IPv4 换票会分到 live-dtocnc-cdn，该节点对这一批票整体 403（用 IPv6 去取
同一个地址照样 403）；用 IPv6 换票分到 hlsliveali-cdn，正常 200。
所以「清单 403」很多时候不是出口被限流，而是票落在一个坏节点上——
同族重试一百次也还是同一个节点，只有换族重换票才能绕开。

不联网，全部 mock。
"""

from __future__ import annotations

import unittest
from unittest import mock

from iptvhd import ysp
from iptvhd.net import Response


def _manifest() -> bytes:
    return b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.000,\nseg1.ts\n"


class RoundModeTest(unittest.TestCase):
    """轮换顺序：本族先换两张票，再换族。"""

    def test_ipv6_tries_same_family_then_falls_back(self):
        seq = [ysp._round_mode("ipv6", i) for i in range(4)]
        self.assertEqual(seq, ["ipv6", "ipv6", "ipv4", "ipv4"])

    def test_ipv4_tries_same_family_then_falls_back(self):
        seq = [ysp._round_mode("ipv4", i) for i in range(3)]
        self.assertEqual(seq, ["ipv4", "ipv4", "ipv6"])

    def test_auto_gives_bottom_layer_first_chance(self):
        self.assertEqual(ysp._round_mode("auto", 0), "auto")
        self.assertIn("ipv4", [ysp._round_mode("auto", i) for i in range(4)])


class RotateTest(unittest.TestCase):
    """端到端：清单 403 时真的换族重换票，而不是同族重试。"""

    def _run(self, *, base: str, bad_rounds: int, attempts: int = 3):
        calls: list[str] = []
        state = {"round": 0}

        def fake_play_info(cnlid, livepid, defn="fhd", client=None,
                           force_ipv4=False, ip_mode="auto"):
            state["round"] += 1
            calls.append(ip_mode)
            host = "bad-node.example" if state["round"] <= bad_rounds else "good-node.example"
            return {"playurl": f"https://{host}/tok/cctv9.m3u8"}

        class FakeClient:
            def __init__(self, **kw):
                self.ip_mode = kw.get("ip_mode", "auto")

            def with_mode(self, mode):
                return FakeClient(ip_mode=mode)

            def get(self, url, **kw):
                ok = "good-node" in url
                return Response(
                    url=url,
                    status=200 if ok else 403,
                    headers={"content-type": "application/vnd.apple.mpegurl"},
                    body=_manifest() if ok else b"denied",
                )

        with mock.patch.object(ysp, "play_info", fake_play_info), \
             mock.patch.object(ysp, "HttpClient", FakeClient), \
             mock.patch.object(ysp, "pace", lambda *a, **k: None), \
             mock.patch.object(ysp.time, "sleep", lambda *a, **k: None):
            url, _info, first = ysp.resolve("2024078601", "600004078",
                                           ip_mode=base, attempts=attempts)
        return url, calls, first

    def test_second_round_retries_same_family(self):
        url, calls, _ = self._run(base="ipv6", bad_rounds=1)
        self.assertIn("good-node", url)
        self.assertEqual(calls, ["ipv6", "ipv6"], "第二次应仍在本族换一张新票")

    def test_falls_back_to_other_family_after_two_failures(self):
        url, calls, _ = self._run(base="ipv6", bad_rounds=2)
        self.assertIn("good-node", url)
        self.assertEqual(calls, ["ipv6", "ipv6", "ipv4"],
                         "本族两张票都不行之后必须换族，否则永远是同一个坏节点")

    def test_first_segment_follows_resolved_token(self):
        url, _calls, first = self._run(base="ipv6", bad_rounds=0)
        self.assertTrue(first.startswith("https://good-node.example/tok/"), first)


if __name__ == "__main__":
    unittest.main()
