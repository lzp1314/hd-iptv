"""央视频强制 IPv4 开关测试：默认关、配置读取、连接只用 AF_INET。

不联网。验证的是「开关只在央视频路径生效、默认行为没被改变」。
"""

from __future__ import annotations

import http.client
import os
import socket
import unittest
from pathlib import Path
from unittest import mock

from iptvhd import ysp
from iptvhd.net import HttpClient, Response, _V4HTTPConnection, _V4HTTPSConnection
from iptvhd.settings import Settings


class SettingsTest(unittest.TestCase):
    def test_default_is_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings(root=Path("."), data={})
            self.assertFalse(s.ysp_ipv4)

    def test_config_ysp_ipv4_true(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings(root=Path("."), data={"ysp": {"ipv4": True}})
            self.assertTrue(s.ysp_ipv4)

    def test_env_overrides_config(self):
        with mock.patch.dict(os.environ, {"HDIPTV_YSP_IPV4": "0"}, clear=True):
            s = Settings(root=Path("."), data={"ysp": {"ipv4": True}})
            self.assertFalse(s.ysp_ipv4)

    def test_env_true_enables(self):
        for value in ("1", "true", "yes"):
            with mock.patch.dict(os.environ, {"HDIPTV_YSP_IPV4": value}, clear=True):
                s = Settings(root=Path("."), data={})
                self.assertTrue(s.ysp_ipv4, value)


class ClientTest(unittest.TestCase):
    def test_force_ipv4_default_off(self):
        self.assertFalse(HttpClient().force_ipv4)

    def test_force_ipv4_can_be_set(self):
        self.assertTrue(HttpClient(force_ipv4=True).force_ipv4)

    def test_plain_client_uses_standard_connection(self):
        from urllib.parse import urlsplit

        parts = urlsplit("https://bkliveinfo.ysp.cctv.cn/x")
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该解析")):
            conn = HttpClient()._connect(parts)
        self.assertIs(type(conn), http.client.HTTPSConnection)

    def test_v4_client_uses_v4_connection(self):
        from urllib.parse import urlsplit

        parts = urlsplit("https://bkliveinfo.ysp.cctv.cn/x")
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("不该解析")):
            conn = HttpClient(force_ipv4=True)._connect(parts)
        self.assertIs(type(conn), _V4HTTPSConnection)

    def test_v4_connect_calls_getaddrinfo_with_af_inet(self):
        # 拦截 socket 层，确认解析只用 AF_INET、且 host 仍是域名（保住 SNI/Host）
        seen = {}

        def fake_getaddrinfo(host, port, family, socktype):
            seen["host"] = host
            seen["family"] = family
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        fake_sock = mock.MagicMock()
        conn = _V4HTTPSConnection("bkliveinfo.ysp.cctv.cn", 443, timeout=3)
        with mock.patch("socket.getaddrinfo", side_effect=fake_getaddrinfo), \
             mock.patch("socket.socket", return_value=fake_sock), \
             mock.patch.object(conn._context, "wrap_socket", return_value=fake_sock) as wrap:
            conn.connect()

        self.assertEqual(seen["family"], socket.AF_INET)
        self.assertEqual(seen["host"], "bkliveinfo.ysp.cctv.cn")
        fake_sock.connect.assert_called_once_with(("127.0.0.1", 443))
        # SNI 必须是域名，换成 IP 会拿到 CDN 的错误节点
        wrap.assert_called_once()
        self.assertEqual(wrap.call_args.kwargs.get("server_hostname"), "bkliveinfo.ysp.cctv.cn")


class YspPathTest(unittest.TestCase):
    def test_resolve_passes_force_ipv4_to_created_client(self):
        # resolve 自建 client 时必须把开关带上；用假客户端打断在换票前，只看创建参数
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with mock.patch.object(ysp, "HttpClient", FakeClient), \
             mock.patch.object(ysp, "pace"), \
             mock.patch.object(ysp, "play_info", side_effect=ysp.YspError("停")), \
             mock.patch("time.sleep"):
            with self.assertRaises(ysp.YspError):
                ysp.resolve("cnlid", "livepid", attempts=1, force_ipv4=True)
        self.assertTrue(captured.get("force_ipv4"))

    def test_play_info_passes_force_ipv4_to_created_client(self):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def get(self, *a, **kw):
                return Response(status=0, error="停")

        with mock.patch.object(ysp, "HttpClient", FakeClient), \
             mock.patch.object(ysp, "pace"):
            with self.assertRaises(ysp.YspError):
                ysp.play_info("cnlid", "livepid", force_ipv4=True)
        self.assertTrue(captured.get("force_ipv4"))


if __name__ == "__main__":
    unittest.main()
