"""模块级地址族三态测试：配置归一化、客户端选类、403 换族重试。

不联网。验证的是「默认 auto 零影响、官方源不许 IPv4、403 能换族」。
"""

from __future__ import annotations

import http.client
import os
import socket
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from iptvhd import settings as settings_mod
from iptvhd import ysp
from iptvhd.net import (
    HttpClient,
    Response,
    _V4HTTPConnection,
    _V4HTTPSConnection,
    _V6HTTPConnection,
    _V6HTTPSConnection,
)
from iptvhd.settings import Settings, coerce_ip_mode, coerce_panel


class CoerceTest(unittest.TestCase):
    def test_valid_modes(self):
        for mode in ("auto", "ipv4", "ipv6"):
            self.assertEqual(coerce_ip_mode(mode), mode)

    def test_invalid_falls_back_to_auto(self):
        for mode in ("", "IPV9", "4", None, 123):
            self.assertEqual(coerce_ip_mode(mode), "auto")

    def test_official_source_cannot_be_ipv4(self):
        self.assertEqual(coerce_ip_mode("ipv4", "运营商官方源"), "auto")
        # 底层源名也归一到模块再判断
        self.assertEqual(coerce_ip_mode("ipv4", "官方直连"), "auto")
        self.assertEqual(coerce_ip_mode("ipv4", "本省自动源"), "auto")

    def test_other_modules_allow_ipv4(self):
        for name in ("央视频", "咪咕视频", "第三方聚合源"):
            self.assertEqual(coerce_ip_mode("ipv4", name), "ipv4")

    def test_coerce_panel_normalizes_map(self):
        raw = {"module_ip_mode": {
            "央视频": "ipv6",
            "综合频道": "ipv4",          # 底层名 → 归一到央视频，覆盖上面的值
            "运营商官方源": "ipv4",       # 被打回 auto
            "未知模块": "xxx",
        }}
        out = coerce_panel(raw)["module_ip_mode"]
        self.assertEqual(out["央视频"], "ipv4")
        self.assertEqual(out["运营商官方源"], "auto")
        self.assertEqual(out["未知模块"], "auto")


class SettingsTest(unittest.TestCase):
    def test_default_all_auto(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings(root=Path("."), data={})
            self.assertEqual(set(s.module_ip_modes().values()), {"auto"})

    def test_panel_overrides_env(self):
        with mock.patch.dict(os.environ, {"HDIPTV_YSP_IP_MODE": "ipv6"}, clear=True):
            s = Settings(root=Path("."), data={},
                         panel={"module_ip_mode": {"央视频": "ipv4"}})
            self.assertEqual(s.module_ip_mode("央视频"), "ipv4")

    def test_env_for_each_module(self):
        env = {
            "HDIPTV_YSP_IP_MODE": "ipv4",
            "HDIPTV_MIGU_IP_MODE": "ipv6",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            s = Settings(root=Path("."), data={})
            self.assertEqual(s.module_ip_mode("央视频"), "ipv4")
            self.assertEqual(s.module_ip_mode("咪咕视频"), "ipv6")
            # 底层源名也能查
            self.assertEqual(s.module_ip_mode("综合频道"), "ipv4")

    def test_env_ipv4_for_official_ignored(self):
        with mock.patch.dict(os.environ, {"HDIPTV_OFFICIAL_IP_MODE": "ipv4"}, clear=True):
            s = Settings(root=Path("."), data={})
            self.assertEqual(s.module_ip_mode("运营商官方源"), "auto")

    def test_legacy_ysp_ipv4_compat(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings(root=Path("."), data={"ysp": {"ipv4": True}})
            self.assertEqual(s.module_ip_mode("央视频"), "ipv4")


class ClientModeTest(unittest.TestCase):
    def test_ip_mode_default_auto(self):
        self.assertEqual(HttpClient().ip_mode, "auto")

    def test_ip_mode_values(self):
        for mode in ("auto", "ipv4", "ipv6"):
            self.assertEqual(HttpClient(ip_mode=mode).ip_mode, mode)

    def test_invalid_mode_falls_back(self):
        self.assertEqual(HttpClient(ip_mode="weird").ip_mode, "auto")

    def test_force_ipv4_compat(self):
        c = HttpClient(force_ipv4=True)
        self.assertEqual(c.ip_mode, "ipv4")
        self.assertTrue(c.force_ipv4)

    def _parts(self):
        return urlsplit("https://bkliveinfo.ysp.cctv.cn/x")

    def test_auto_uses_standard(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该解析")):
            conn = HttpClient(ip_mode="auto")._connect(self._parts())
        self.assertIs(type(conn), http.client.HTTPSConnection)

    def test_ipv4_uses_v4_class(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该解析")):
            conn = HttpClient(ip_mode="ipv4")._connect(self._parts())
        self.assertIs(type(conn), _V4HTTPSConnection)

    def test_ipv6_uses_v6_class(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该解析")):
            conn = HttpClient(ip_mode="ipv6")._connect(self._parts())
        self.assertIs(type(conn), _V6HTTPSConnection)

    def test_forced_mode_overrides(self):
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该解析")):
            conn = HttpClient(ip_mode="auto")._connect(self._parts(), forced_mode="ipv4")
        self.assertIs(type(conn), _V4HTTPSConnection)

    def test_v6_connect_uses_af_inet6(self):
        seen = {}
        def fake_getaddrinfo(host, port, family, socktype):
            seen.update(host=host, family=family)
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))]
        fake_sock = mock.MagicMock()
        conn = _V6HTTPSConnection("bkliveinfo.ysp.cctv.cn", 443, timeout=3)
        with mock.patch("socket.getaddrinfo", side_effect=fake_getaddrinfo), \
             mock.patch("socket.socket", return_value=fake_sock), \
             mock.patch.object(conn._context, "wrap_socket", return_value=fake_sock):
            conn.connect()
        self.assertEqual(seen["family"], socket.AF_INET6)
        self.assertEqual(seen["host"], "bkliveinfo.ysp.cctv.cn")


class SwitchOn403Test(unittest.TestCase):
    """auto 模式拿到 403 时换另一族重试一次。"""

    def test_403_ipv6_switches_to_ipv4_success(self):
        c = HttpClient(ip_mode="auto")
        first = Response(url="https://x/y", status=403, body=b"", family=socket.AF_INET6)
        second = Response(url="https://x/y", status=200, body=b"ok")
        calls = []
        def fake_loop(url, **kw):
            calls.append(kw.get("forced_mode", ""))
            return first if len(calls) == 1 else second
        with mock.patch.object(c, "_get_loop", side_effect=fake_loop):
            out = c.get("https://x/y")
        self.assertEqual(out.status, 200)
        self.assertEqual(calls, ["", "ipv4"])

    def test_403_ipv4_switches_to_ipv6(self):
        c = HttpClient(ip_mode="auto")
        first = Response(url="https://x/y", status=403, body=b"", family=socket.AF_INET)
        calls = []
        def fake_loop(url, **kw):
            calls.append(kw.get("forced_mode", ""))
            return first
        with mock.patch.object(c, "_get_loop", side_effect=fake_loop):
            c.get("https://x/y")
        self.assertEqual(calls, ["", "ipv6"])

    def test_non_auto_never_switches(self):
        c = HttpClient(ip_mode="ipv4")
        first = Response(url="https://x/y", status=403, body=b"", family=socket.AF_INET)
        calls = []
        with mock.patch.object(c, "_get_loop", side_effect=lambda u, **kw: (calls.append(1), first)[1]):
            c.get("https://x/y")
        self.assertEqual(len(calls), 1)

    def test_403_switch_failure_keeps_first(self):
        c = HttpClient(ip_mode="auto")
        first = Response(url="https://x/y", status=403, body=b"", error="原始拒绝", family=socket.AF_INET6)
        second = Response(url="https://x/y", status=403, body=b"", error="换族也拒绝")
        seq = [first, second]
        with mock.patch.object(c, "_get_loop", side_effect=lambda u, **kw: seq.pop(0)):
            out = c.get("https://x/y")
        self.assertEqual(out.error, "原始拒绝")


class YspPathTest(unittest.TestCase):
    def test_resolve_passes_ip_mode(self):
        captured = {}
        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
        with mock.patch.object(ysp, "HttpClient", FakeClient), \
             mock.patch.object(ysp, "pace"), \
             mock.patch.object(ysp, "play_info", side_effect=ysp.YspError("停")), \
             mock.patch("time.sleep"):
            with self.assertRaises(ysp.YspError):
                ysp.resolve("cnlid", "livepid", attempts=1, ip_mode="ipv6")
        self.assertEqual(captured.get("ip_mode"), "ipv6")

    def test_play_info_passes_ip_mode(self):
        captured = {}
        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def get(self, *a, **kw):
                return Response(status=0, error="停")
        with mock.patch.object(ysp, "HttpClient", FakeClient), \
             mock.patch.object(ysp, "pace"):
            with self.assertRaises(ysp.YspError):
                ysp.play_info("cnlid", "livepid", ip_mode="ipv4")
        self.assertEqual(captured.get("ip_mode"), "ipv4")


if __name__ == "__main__":
    unittest.main()
