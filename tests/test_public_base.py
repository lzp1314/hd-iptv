"""对外地址推断。

反代进来的请求要拿到外网域名，内网直连要拿到内网地址——两者混了就会出现
「从外网打开面板，订阅地址却是 192.168.x.x」这种看着就没法用的问题。
"""

from __future__ import annotations

import unittest

from iptvhd.service import _SEEN_PUBLIC, _looks_local, _public_base


class TestLooksLocal(unittest.TestCase):
    def test_lan_addresses(self):
        for host in (
            "192.168.10.2:9797", "127.0.0.1", "10.0.0.5", "172.16.3.2",
            "172.31.9.9", "localhost:9797", "nas.local", "box.lan", "[::1]:9797",
        ):
            self.assertTrue(_looks_local(host), host)

    def test_public_addresses(self):
        for host in ("iptv.example.com", "tv.example.com:8443", "8.8.8.8", "172.32.0.1"):
            self.assertFalse(_looks_local(host), host)


class TestPublicBase(unittest.TestCase):
    def setUp(self):
        _SEEN_PUBLIC["base"] = ""
        _SEEN_PUBLIC["at"] = 0.0

    def test_host_alone_is_used(self):
        self.assertEqual(
            _public_base({"Host": "192.168.10.2:9797"}),
            "http://192.168.10.2:9797",
        )

    def test_forwarded_host_wins_over_host(self):
        base = _public_base({
            "Host": "192.168.10.2:9797",
            "X-Forwarded-Host": "iptv.liuzhangnas.cc.cd:16666",
            "X-Forwarded-Proto": "https",
        })
        self.assertEqual(base, "https://iptv.liuzhangnas.cc.cd:16666")

    def test_rfc7239_forwarded_header(self):
        base = _public_base({
            "Host": "192.168.10.2:9797",
            "Forwarded": "for=1.2.3.4;host=tv.example.com;proto=https",
            "X-Forwarded-Proto": "https",
        })
        self.assertEqual(base, "https://tv.example.com")

    def test_manual_public_url_wins(self):
        base = _public_base(
            {"Host": "192.168.10.2:9797", "X-Forwarded-Host": "iptv.example.com"},
            "https://tv.example.com/",
        )
        self.assertEqual(base, "https://tv.example.com")

    def test_lan_direct_stays_lan(self):
        """内网直接访问时不能自作聪明换成外网域名——那要绕出去一圈。"""
        _public_base({"X-Forwarded-Host": "iptv.example.com", "X-Forwarded-Proto": "https"})
        self.assertEqual(
            _public_base({"Host": "192.168.10.2:9797"}),
            "http://192.168.10.2:9797",
        )

    def test_proxy_rewriting_host_falls_back_to_seen(self):
        """反代把 Host 改回内网、又不传 XFH 时，用之前见过的外网地址。"""
        _public_base({"X-Forwarded-Host": "iptv.example.com", "X-Forwarded-Proto": "https"})
        base = _public_base({"Host": "192.168.10.2:9797", "X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(base, "https://iptv.example.com")

    def test_seen_expires(self):
        _SEEN_PUBLIC["base"] = "https://old.example.com"
        _SEEN_PUBLIC["at"] = 0.0
        self.assertEqual(
            _public_base({"Host": "192.168.10.2:9797", "X-Forwarded-For": "1.2.3.4"}),
            "http://192.168.10.2:9797",
        )


if __name__ == "__main__":
    unittest.main()
