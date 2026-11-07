"""综合频道协议层测试：编码、分片拼接、结果解析。

这些用例不联网，验证的是「协议实现没被改坏」，而不是接口还活着——
接口的变化只能靠真实运行发现。
"""

from __future__ import annotations

import base64
import struct
import unittest
from unittest import mock

from iptvhd import ysp
from iptvhd.net import Response


class TeaTest(unittest.TestCase):
    def test_block_is_16_rounds_and_reversible_shape(self):
        key = bytes(range(16))
        block = bytes(range(8, 16))
        out = ysp._encrypt_block(block, key)
        self.assertEqual(len(out), 8)
        self.assertNotEqual(out, block)

    def test_block_is_deterministic(self):
        key = ysp._TEA_KEY
        block = b"12345678"
        self.assertEqual(ysp._encrypt_block(block, key), ysp._encrypt_block(block, key))

    def test_packet_length_is_block_aligned(self):
        for size in (0, 1, 7, 8, 9, 40, 257):
            data = b"x" * size
            out = ysp._encrypt_packet(data, ysp._TEA_KEY)
            self.assertEqual(len(out) % 8, 0, f"size={size}")
            self.assertGreaterEqual(len(out), size + 10)

    def test_packet_is_not_plaintext(self):
        data = b"CCTV1-SECRET-MARKER"
        out = ysp._encrypt_packet(data, ysp._TEA_KEY)
        self.assertNotIn(b"SECRET", out)

    def test_checksum_matches_java_style_roll(self):
        self.assertEqual(ysp._checksum(b""), 0)
        self.assertEqual(ysp._checksum(b"\x01"), 1)
        self.assertEqual(ysp._checksum(b"\x01\x02"), 131 + 2)


class CkeyTest(unittest.TestCase):
    def setUp(self):
        self.token = ysp.make_ckey("2024078201")

    def test_has_expected_fields(self):
        for key in ("cKey", "guid", "ts", "flowId"):
            self.assertIn(key, self.token)
        self.assertTrue(self.token["cKey"].startswith("--01"))

    def test_ckey_is_url_safe(self):
        # 标准 base64 的 + / = 都被替换掉了，可以直接塞进 query
        body = self.token["cKey"][4:]
        self.assertTrue(all(c.isalnum() or c in "-_" for c in body), body[:40])
        self.assertNotIn("=", body)

    def test_ckey_differs_each_call(self):
        other = ysp.make_ckey("2024078201")
        self.assertNotEqual(self.token["cKey"], other["cKey"])

    def test_base64_decodes(self):
        body = self.token["cKey"][4:]
        pad = "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(body + pad)
        # 结构：8 字节对齐的密文 + 4 字节校验和
        self.assertEqual((len(raw) - 4) % 8, 0)
        self.assertGreater(len(raw), 100)


class SegUrlTest(unittest.TestCase):
    def test_token_prefix_is_kept(self):
        playlist = "https://cdn.example.com/ABCDEF/2024078203.m3u8?from=player&pid=1"
        seg = ysp.seg_url(playlist, "2024078203-100.ts")
        self.assertEqual(seg, "https://cdn.example.com/ABCDEF/2024078203-100.ts?from=player&pid=1")

    def test_segment_query_wins(self):
        playlist = "https://cdn.example.com/TOKEN/x.m3u8?a=1"
        seg = ysp.seg_url(playlist, "y.ts?b=2")
        self.assertEqual(seg, "https://cdn.example.com/TOKEN/y.ts?b=2")

    def test_naive_join_would_lose_token(self):
        from urllib.parse import urljoin

        playlist = "https://cdn.example.com/ABCDEF/2024078203.m3u8"
        # 普通相对路径解析会把 token 目录吃掉，这正是接入时踩过的坑
        self.assertEqual(urljoin(playlist, "y.ts"), "https://cdn.example.com/ABCDEF/y.ts")
        self.assertIn("/ABCDEF/", ysp.seg_url(playlist, "y.ts"))


class PlayUrlsTest(unittest.TestCase):
    def test_primary_first_then_backups_deduped(self):
        info = {
            "playurl": "https://a/1.m3u8",
            "backurl_list": [{"url": "https://b/2.m3u8"}, {"url": "https://a/1.m3u8"}, {"url": ""}],
        }
        self.assertEqual(ysp.play_urls(info), ["https://a/1.m3u8", "https://b/2.m3u8"])

    def test_empty(self):
        self.assertEqual(ysp.play_urls({}), [])


class ResolveTest(unittest.TestCase):
    def _resp(self, status: int, body: bytes) -> Response:
        return Response(url="u", status=status, body=body, headers={})

    def test_retries_until_playlist_ok(self):
        playlist = b"#EXTM3U\n#EXTINF:5,\nseg.ts\n"
        client = mock.Mock()
        client.get.side_effect = [
            self._resp(403, b""),          # 第一轮列表 403
            self._resp(200, playlist),     # 第二轮成功
        ]
        info = {"playurl": "https://cdn/t/c.m3u8", "backurl_list": []}
        with mock.patch.object(ysp, "play_info", return_value=info), \
                mock.patch.object(ysp.time, "sleep", return_value=None):
            url, got, first = ysp.resolve("1", "2", "fhd", client, attempts=2, pause=0)
        self.assertEqual(url, "https://cdn/t/c.m3u8")
        self.assertEqual(first, "https://cdn/t/seg.ts")
        self.assertIs(got, info)

    def test_gives_up_after_attempts(self):
        client = mock.Mock()
        client.get.return_value = self._resp(403, b"")
        info = {"playurl": "https://cdn/t/c.m3u8"}
        with mock.patch.object(ysp, "play_info", return_value=info), \
                mock.patch.object(ysp.time, "sleep", return_value=None):
            with self.assertRaises(ysp.YspError):
                ysp.resolve("1", "2", "fhd", client, attempts=2, pause=0)

    def test_unavailable_is_not_retried(self):
        client = mock.Mock()
        with mock.patch.object(ysp, "play_info",
                               side_effect=ysp.YspUnavailable("iretcode=12")):
            with self.assertRaises(ysp.YspUnavailable):
                ysp.resolve("1", "2", "fhd", client, attempts=3, pause=0)
        client.get.assert_not_called()


class ChannelTableTest(unittest.TestCase):
    def test_table_is_consistent(self):
        slugs = [row[0] for row in ysp.CHANNELS]
        self.assertEqual(len(slugs), len(set(slugs)), "slug 有重复")
        for slug, name, cnlid, livepid, defn in ysp.CHANNELS:
            self.assertTrue(slug.islower())
            self.assertTrue(cnlid.isdigit(), slug)
            self.assertTrue(livepid.isdigit(), slug)
            self.assertIn(defn, ysp.DEFN_HEIGHT, slug)

    def test_covers_cctv_and_satellite(self):
        groups = {ysp_group(row[0]) for row in ysp.CHANNELS}
        self.assertEqual(groups, {"cctv", "ws"})
        self.assertGreaterEqual(sum(1 for r in ysp.CHANNELS if ysp_group(r[0]) == "cctv"), 18)
        self.assertGreaterEqual(sum(1 for r in ysp.CHANNELS if ysp_group(r[0]) == "ws"), 30)


def ysp_group(slug: str) -> str:
    return "cctv" if slug.startswith(("cctv", "cgtn")) else "ws"


if __name__ == "__main__":
    unittest.main()
