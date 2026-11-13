"""运营商自测（``doctor --isp``）的测试。

这个命令的价值全在「结论别把人带沟里」，所以测试重点不是网络（那不可控），
而是：清单解析对不对、四种清单状态怎么说、结论分支会不会自相矛盾。
"""

from __future__ import annotations

import socket
import unittest
from unittest import mock

from iptvhd import ispcheck


class TestParseEndpoints(unittest.TestCase):
    def test_txt_style(self):
        text = (
            "央视,#genre#\n"
            "CCTV1,http://ztecdn.hb.chinamobile.com:6060/170000001115/a/index.m3u8\n"
            "CCTV2,http://huaweicdn.hb.chinamobile.com/PLTV/1.m3u8\n"
        )
        self.assertEqual(
            ispcheck.parse_endpoints(text),
            [("ztecdn.hb.chinamobile.com", 6060), ("huaweicdn.hb.chinamobile.com", 80)],
        )

    def test_m3u_style_and_dedupe(self):
        text = (
            "#EXTM3U\n#EXTINF:-1,CCTV1\nhttp://a.example.com:8080/x.m3u8\n"
            "#EXTINF:-1,CCTV2\nhttp://a.example.com:8080/y.m3u8\n"
        )
        self.assertEqual(ispcheck.parse_endpoints(text), [("a.example.com", 8080)])

    def test_default_ports(self):
        text = "http://a.example.com/x\nhttps://b.example.com/y\n"
        self.assertEqual(
            ispcheck.parse_endpoints(text),
            [("a.example.com", 80), ("b.example.com", 443)],
        )

    def test_skips_multicast_and_garbage(self):
        # 组播地址要 udpxy 转，当成单播入口测只会永远失败
        text = "CCTV1,rtp://239.1.1.1:5000\nCCTV2,udp://239.1.1.2:5000\n没有地址\n"
        self.assertEqual(ispcheck.parse_endpoints(text), [])

    def test_private_hidden_by_default(self):
        text = "http://10.1.2.3:8080/a.m3u8\nhttp://192.168.1.9/b.m3u8\nhttp://ok.example.com/c\n"
        self.assertEqual(ispcheck.parse_endpoints(text), [("ok.example.com", 80)])
        kept = ispcheck.parse_endpoints(text, include_private=True)
        self.assertEqual(len(kept), 3)
        self.assertTrue(ispcheck.is_private_host("10.1.2.3"))
        self.assertFalse(ispcheck.is_private_host("ok.example.com"))

    def test_limit(self):
        text = "\n".join(f"http://h{i}.example.com/x" for i in range(10))
        self.assertEqual(len(ispcheck.parse_endpoints(text, limit=3)), 3)


class TestListNote(unittest.TestCase):
    def test_missing(self):
        self.assertIn("没有这一份", ispcheck.list_note({"list": "missing"}))

    def test_fail(self):
        self.assertIn("取不到", ispcheck.list_note({"list": "fail"}))

    def test_with_endpoints(self):
        note = ispcheck.list_note({"list": "ok", "endpoints": 5, "private_entries": 0})
        self.assertIn("5 条", note)

    def test_private_only(self):
        note = ispcheck.list_note({"list": "ok", "endpoints": 0, "private_entries": 12})
        self.assertIn("内网地址 12 条", note)

    def test_placeholder_only(self):
        note = ispcheck.list_note({"list": "ok", "endpoints": 0, "private_entries": 0})
        self.assertIn("只有频道名", note)


def _row(host="a.example.com", port=80, status="ok", ms=10, code=None):
    return {"host": host, "port": port, "status": status, "ms": ms, "code": code}


class TestVerdict(unittest.TestCase):
    def _info(self, ipv6=True):
        return {"province": "hubei", "isp": "mobile", "ipv6": ipv6}

    def test_reachable_says_worth_it(self):
        lines = ispcheck.verdict(self._info(), {
            "province": "hubei", "isp": "mobile", "list": "ok",
            "endpoints": 1, "private_entries": 0, "rows": [_row()],
        })
        self.assertIn("值得接", "\n".join(lines))

    def test_broadnet_mentions_coax(self):
        lines = ispcheck.verdict(
            {"province": "hubei", "isp": "broadnet", "ipv6": True},
            {"province": "hubei", "isp": "broadnet", "list": "missing",
             "endpoints": 0, "private_entries": 0, "rows": []},
        )
        text = "\n".join(lines)
        self.assertIn("同轴", text)
        self.assertIn("没有可抓的 IP 单播源", text)

    def test_missing_list(self):
        lines = ispcheck.verdict(self._info(), {
            "province": "hubei", "isp": "mobile", "list": "missing",
            "endpoints": 0, "private_entries": 0, "rows": [_row(status="timeout")],
        })
        self.assertIn("没有这个省份", "\n".join(lines))

    def test_private_only_list_points_at_stb(self):
        lines = ispcheck.verdict(self._info(), {
            "province": "hubei", "isp": "mobile", "list": "ok",
            "endpoints": 0, "private_entries": 8, "rows": [],
        })
        self.assertIn("内网地址", "\n".join(lines))

    def test_placeholder_list(self):
        lines = ispcheck.verdict(self._info(), {
            "province": "hubei", "isp": "mobile", "list": "ok",
            "endpoints": 0, "private_entries": 0, "rows": [],
        })
        text = "\n".join(lines)
        self.assertIn("只填了频道名", text)
        self.assertIn("机顶盒", text)

    def test_dns_failure(self):
        lines = ispcheck.verdict(self._info(), {
            "province": "hubei", "isp": "mobile", "list": "fail",
            "endpoints": 0, "private_entries": 0, "rows": [_row(status="dns")],
        })
        self.assertIn("解析不了", "\n".join(lines))

    def test_timeout_without_ipv6(self):
        lines = ispcheck.verdict(self._info(ipv6=False), {
            "province": "hubei", "isp": "mobile", "list": "fail",
            "endpoints": 0, "private_entries": 0, "rows": [_row(status="timeout")],
        })
        self.assertIn("IPv6", "\n".join(lines))

    def test_unknown_region_falls_back(self):
        lines = ispcheck.verdict({}, {"rows": [], "list": "fail"})
        self.assertIn("这台机器的宽带", "\n".join(lines))


class TestTcpProbe(unittest.TestCase):
    def test_dns_failure(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror):
            self.assertEqual(ispcheck.tcp_probe("nope.invalid", 80, 1.0), ("dns", 0.0))

    def test_connect_refused(self):
        fake = mock.MagicMock()
        fake.connect.side_effect = ConnectionRefusedError
        fake.__enter__ = mock.Mock(return_value=fake)
        fake.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 80))]), \
             mock.patch.object(socket, "socket", return_value=fake):
            self.assertEqual(ispcheck.tcp_probe("a.example.com", 80, 1.0)[0], "refused")


class TestCheck(unittest.TestCase):
    def test_merges_known_and_list(self):
        text = "CCTV1,http://a.example.com:8080/x.m3u8\nCCTV2,http://10.0.0.5/y.m3u8\n"
        with mock.patch.object(ispcheck, "fetch_list", return_value=(text, "ok")), \
             mock.patch.object(ispcheck, "tcp_probe", return_value=("ok", 12.0)), \
             mock.patch.object(ispcheck, "http_status", return_value=302):
            result = ispcheck.check("hubei", "mobile", timeout=1.0)
        hosts = [r["host"] for r in result["rows"]]
        self.assertIn("ztecdn.hb.chinamobile.com", hosts)   # 内置表
        self.assertIn("a.example.com", hosts)               # 公开清单
        self.assertNotIn("10.0.0.5", hosts)                 # 内网地址只计数不探测
        self.assertEqual(result["private_entries"], 1)
        self.assertEqual(result["endpoints"], 1)

    def test_no_known_host_and_no_list(self):
        with mock.patch.object(ispcheck, "fetch_list", return_value=("", "missing")):
            result = ispcheck.check("hubei", "broadnet", timeout=1.0)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["list"], "missing")


class TestRun(unittest.TestCase):
    def _run(self, info, result, argv_isp=""):
        out: list[str] = []
        with mock.patch.object(ispcheck.region, "detect", return_value=info), \
             mock.patch.object(ispcheck, "check", return_value=result):
            code = ispcheck.run(
                ".", manual_province=argv_isp, manual_isp="", echo=out.append,
            )
        return code, "\n".join(out)

    def test_ok_returns_zero(self):
        info = {"ok": True, "province": "hubei", "isp": "mobile",
                "province_name": "湖北", "isp_name": "中国移动", "ipv6": True,
                "ip": "1.2.3.4", "source": "ipip"}
        result = {"province": "hubei", "isp": "mobile", "list": "ok",
                  "endpoints": 1, "private_entries": 0, "rows": [_row()]}
        code, text = self._run(info, result)
        self.assertEqual(code, 0)
        self.assertIn("湖北 · 中国移动", text)
        self.assertIn("值得接", text)

    def test_unreachable_returns_three(self):
        info = {"ok": True, "province": "hubei", "isp": "mobile",
                "province_name": "湖北", "isp_name": "中国移动", "ipv6": False,
                "source": "ipip"}
        result = {"province": "hubei", "isp": "mobile", "list": "missing",
                  "endpoints": 0, "private_entries": 0, "rows": [_row(status="timeout")]}
        code, text = self._run(info, result)
        self.assertEqual(code, 3)
        self.assertIn("超时", text)

    def test_unknown_region_stops_early(self):
        info = {"ok": False, "province": "", "isp": "", "ipv6": False, "source": ""}
        code, text = self._run(info, {"rows": []})
        self.assertEqual(code, 3)
        self.assertIn("手动指定", text)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _n):
        return b"CCTV1,http://1.2.3.4/a.m3u8\n"


class TestFetchListRetry(unittest.TestCase):
    """国内访问 GitHub raw / jsDelivr 的首次 TLS 抖动不该被当成「没有这一份」。"""

    def test_transient_error_retries_same_url(self):
        import urllib.error

        with mock.patch.object(
            ispcheck.urllib.request, "urlopen",
            side_effect=[urllib.error.URLError("tls"), _Resp()],
        ) as opened:
            text, state = ispcheck.fetch_list("anhui", "mobile")
        self.assertEqual(state, "ok")
        self.assertIn("CCTV1", text)
        self.assertEqual(opened.call_count, 2)

    def test_404_is_missing_not_fail(self):
        import urllib.error

        err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
        with mock.patch.object(ispcheck.urllib.request, "urlopen", side_effect=err) as opened:
            _, state = ispcheck.fetch_list("nowhere", "mobile")
        self.assertEqual(state, "missing")
        self.assertEqual(opened.call_count, 2, "两个镜像各确认一次")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
