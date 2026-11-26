"""央视频换票的地址族轮换：清单取不到时必须换一条链路重换票。

背景（2026-11-26 实测修正）：CDN 节点**不是**按取票时的出口族分配的——用 IPv4
取票分到过 hlsliveali，用 IPv6 取票拿到过 dtocnc，分配是随机的。所以这里验证的
不是「族 → 节点」的对应关系，而是一条更朴素的性质：

    某条地址族在 NAS 这条线路上取不到东西时（超时 / 403 / 清单读一半断），
    同族重来多少次都是同样结果，唯一出路是换另一族重取一张票。

历史 bug：``("auto", "ipv6", "ipv6", "ipv4")`` 四级台阶配 ``attempts=3``，
后两轮重复同一族，IPv4 永远轮不到。面板选「自适应」时一旦 IPv6 不通就必然全灭，
选「仅 IPv4」反而能用。

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
    """轮换顺序：本族先换两张票，再换族；auto 必须在 attempts 内摸到两族。"""

    def test_ipv6_tries_same_family_then_falls_back(self):
        seq = [ysp._round_mode("ipv6", i) for i in range(4)]
        self.assertEqual(seq, ["ipv6", "ipv6", "ipv4", "ipv4"])

    def test_ipv4_tries_same_family_then_falls_back(self):
        seq = [ysp._round_mode("ipv4", i) for i in range(3)]
        self.assertEqual(seq, ["ipv4", "ipv4", "ipv6"])

    def test_auto_gives_bottom_layer_first_chance(self):
        self.assertEqual(ysp._round_mode("auto", 0), "auto")

    def test_auto_reaches_ipv4_within_three_attempts(self):
        """auto 的三轮必须覆盖到 IPv4。

        线上调用是 ``attempts=3``（service._ysp_resolver），所以「第 4 轮才换族」
        等于永远不换。这条断言就是那个 bug 的围栏。
        """
        seq = [ysp._round_mode("auto", i) for i in range(3)]
        self.assertEqual(seq, ["auto", "ipv4", "ipv6"])
        self.assertIn("ipv4", seq)
        self.assertIn("ipv6", seq)

    def test_auto_does_not_repeat_a_family(self):
        seq = [ysp._round_mode("auto", i) for i in range(3)]
        self.assertEqual(len(set(seq[1:])), 2, "后两轮不该是同一个族")


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

    def test_auto_falls_to_ipv4_on_second_round(self):
        """auto 第一轮失败后，第二轮必须换成 IPv4 而不是再来一次 IPv6。"""
        url, calls, _ = self._run(base="auto", bad_rounds=1)
        self.assertIn("good-node", url)
        self.assertEqual(calls, ["auto", "ipv4"])


class YspClientModeTest(unittest.TestCase):
    """面板设的地址族要真的作用到「取清单」这一步。

    以前 ``Relay._ysp_client`` 写死 auto：选「仅 IPv4」只管住了换票，后台每 12 秒
    取清单仍旧先试 IPv6 再回落。设置和实际行为不一致，出故障时连拿它排除都做不到。
    """

    def _relay(self, **kw):
        from iptvhd.relay import Relay

        return Relay(
            lambda slug: ("https://live-dtocnc-cdn.ysp.cctv.cn/x.m3u8", "qqlive"),
            warm=False,
            **kw,
        )

    def test_initial_mode_is_used(self):
        relay = self._relay(ysp_ip_mode="ipv4")
        self.assertEqual(relay._ysp_client().ip_mode, "ipv4")

    def test_configure_switches_and_drops_cached_client(self):
        relay = self._relay(ysp_ip_mode="auto")
        before = relay._ysp_client()
        relay.configure(ysp_ip_mode="ipv6")
        after = relay._ysp_client()
        self.assertIsNot(before, after, "换族必须换客户端，否则族记忆把新设置顶回去")
        self.assertEqual(after.ip_mode, "ipv6")

    def test_same_mode_keeps_client(self):
        relay = self._relay(ysp_ip_mode="ipv4")
        before = relay._ysp_client()
        relay.configure(ysp_ip_mode="ipv4")
        self.assertIs(before, relay._ysp_client(), "没变就别重建，族记忆要留住")

    def test_unknown_value_is_ignored(self):
        relay = self._relay(ysp_ip_mode="ipv4")
        relay.configure(ysp_ip_mode="bogus")
        self.assertEqual(relay._ysp_client().ip_mode, "ipv4")


if __name__ == "__main__":
    unittest.main()
