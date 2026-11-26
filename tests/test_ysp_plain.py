"""央视频分片的交付方式：默认经本机转发，关掉才让播放器直连。

背景（2026-11-26 用户四端实测，手机自研播放器 / 观屿 TV / 电视自研播放器 /
观屿播放器）：分片让播放器直连 CDN，立刻被拒、卡死；改成经本机转发就稳。

差别不在播放器，而在「谁能换地址族」：同一个分片地址，v4 能取、v6 回 403；
本机这一层有族记忆和 403 换族，播放器只按系统偏好建连，落错族就一路 403。
所以默认不让播放器直连。

``relay_ysp=False`` 保留老行为（分片直连、http 降级），供排查用。
"""

from __future__ import annotations

import unittest

from iptvhd.relay import Relay

SEG = "https://live-dtocnc-cdn.ysp.cctv.cn/ABCDEF/seg-1.ts?pid=600001859&cdn=5505"
PREFIX = "http://192.168.1.2:8080"


def _relay(**kwargs) -> Relay:
    return Relay(lambda slug: ("https://cdn.example.com/a.m3u8", "qqlive"), warm=False, **kwargs)


class YspDeliveryTest(unittest.TestCase):
    def test_ysp_segment_goes_through_relay_by_default(self):
        """默认：央视频分片换成 /relay/，播放器不直连 CDN。"""
        relay = _relay()
        out = relay.target("cctv1", SEG, PREFIX)
        self.assertTrue(out.startswith(PREFIX + "/relay/"), out)
        self.assertNotIn("ysp.cctv.cn", out)

    def test_other_hosts_still_direct(self):
        """只有央视频强制转发；其它源在 direct 模式下照旧直连。"""
        relay = _relay()
        other = "https://cdn.example.com/live/seg-1.ts"
        self.assertEqual(relay.target("cctv1", other, PREFIX), other)

    def test_relay_off_restores_http_downgrade(self):
        relay = _relay(relay_ysp=False)
        out = relay.target("cctv1", SEG, PREFIX)
        self.assertTrue(out.startswith("http://live-dtocnc-cdn.ysp.cctv.cn/"), out)
        # 路径与查询串必须原样保留（票据在查询串里，改了就 403）
        self.assertIn("/ABCDEF/seg-1.ts", out)
        self.assertIn("pid=600001859&cdn=5505", out)

    def test_relay_off_keeps_https_when_switch_off(self):
        relay = _relay(relay_ysp=False, plain_http=False)
        self.assertEqual(relay.target("cctv1", SEG, PREFIX), SEG)

    def test_relay_off_https_panel_never_downgrades(self):
        """https 面板里往 http 分片发请求会被浏览器按混合内容拦掉。"""
        relay = _relay(relay_ysp=False)
        self.assertEqual(relay.target("cctv1", SEG, "https://tv.example.com"), SEG)

    def test_proxy_mode_still_wraps(self):
        relay = _relay(mode="proxy")
        self.assertIn("/relay/", relay.target("cctv1", SEG, PREFIX))

    def test_configure_toggles_relay(self):
        relay = _relay()
        self.assertIn("/relay/", relay.target("cctv1", SEG, PREFIX))
        relay.configure(relay_ysp=False)
        self.assertTrue(relay.target("cctv1", SEG, PREFIX).startswith("http://"))
        relay.configure(relay_ysp=True)
        self.assertIn("/relay/", relay.target("cctv1", SEG, PREFIX))


if __name__ == "__main__":
    unittest.main()
