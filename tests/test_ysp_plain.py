"""央视频分片 http 降级：老旧电视盒子的 TLS 兼容。

背景（2026-11-25 实测）：同一台盒子播运营商源、咪咕（走本机转发）都正常，
只有央视频报「网络连接错误」——它是唯一让盒子自己做 TLS 握手去直连 CDN 的源。
而央视频 CDN 的同一个分片地址 https 与 http 返回**逐字节相同**的内容
（2072324 字节，http 不被 302 回 https），所以直连时把 scheme 降成 http 即可，
分片仍由播放器直连 CDN，不占本机带宽、不增加本机对上游的请求。

这几条锁住「什么时候该降、什么时候绝不能降」。
"""

from __future__ import annotations

import unittest

from iptvhd.relay import Relay

SEG = "https://live-dtocnc-cdn.ysp.cctv.cn/ABCDEF/seg-1.ts?pid=600001859&cdn=5505"
PREFIX = "http://192.168.1.2:8080"


def _relay(**kwargs) -> Relay:
    relay = Relay(lambda slug: ("https://cdn.example.com/a.m3u8", "qqlive"), warm=False, **kwargs)
    return relay


class YspPlainTest(unittest.TestCase):
    def test_direct_http_panel_downgrades_ysp_segment(self):
        relay = _relay()
        out = relay.target("cctv1", SEG, PREFIX)
        self.assertTrue(out.startswith("http://live-dtocnc-cdn.ysp.cctv.cn/"))
        # 路径与查询串必须原样保留（票据在查询串里，改了就 403）
        self.assertIn("/ABCDEF/seg-1.ts", out)
        self.assertIn("pid=600001859&cdn=5505", out)

    def test_switch_off_keeps_https(self):
        relay = _relay(plain_http=False)
        self.assertEqual(relay.target("cctv1", SEG, PREFIX), SEG)

    def test_https_panel_never_downgrades(self):
        """https 面板里往 http 分片发请求会被浏览器按混合内容拦掉。"""
        relay = _relay()
        self.assertEqual(relay.target("cctv1", SEG, "https://tv.example.com"), SEG)

    def test_other_hosts_untouched(self):
        relay = _relay()
        other = "https://cdn.example.com/live/seg-1.ts"
        self.assertEqual(relay.target("cctv1", other, PREFIX), other)

    def test_proxy_mode_still_wraps(self):
        relay = _relay(mode="proxy")
        out = relay.target("cctv1", SEG, PREFIX)
        self.assertIn("/relay/", out)
        self.assertNotIn("ysp.cctv.cn", out)

    def test_plain_url_untouched(self):
        relay = _relay()
        plain = "http://live-dtocnc-cdn.ysp.cctv.cn/ABCDEF/seg-1.ts?pid=1"
        self.assertEqual(relay.target("cctv1", plain, PREFIX), plain)

    def test_configure_toggles_switch(self):
        relay = _relay()
        self.assertTrue(relay.target("cctv1", SEG, PREFIX).startswith("http://"))
        relay.configure(plain_http=False)
        self.assertEqual(relay.target("cctv1", SEG, PREFIX), SEG)
        relay.configure(plain_http=True)
        self.assertTrue(relay.target("cctv1", SEG, PREFIX).startswith("http://"))


if __name__ == "__main__":
    unittest.main()
