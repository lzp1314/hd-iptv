"""版本检查的纯逻辑测试：不联网，全部打桩。"""

from __future__ import annotations

import unittest

from iptvhd import updater


class VersionCompareTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version('__version__ = "1.2.3"'), "1.2.3")
        self.assertEqual(updater.parse_version("没有版本号"), "")

    def test_tuple(self):
        self.assertEqual(updater.as_tuple("0.2.0"), (0, 2, 0))
        self.assertEqual(updater.as_tuple(""), (0,))
        self.assertEqual(updater.as_tuple("1.0.0-beta"), (1, 0, 0))

    def test_newer(self):
        self.assertTrue(updater.newer("0.3.0", "0.2.0"))
        self.assertTrue(updater.newer("0.10.0", "0.9.0"))   # 按数字比，不是按字符串
        self.assertFalse(updater.newer("0.2.0", "0.2.0"))
        self.assertFalse(updater.newer("", "0.2.0"))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self._orig_get = updater._get
        updater._cache["at"] = 0.0
        updater._cache["data"] = {}

    def tearDown(self):
        updater._get = self._orig_get
        updater._cache["at"] = 0.0
        updater._cache["data"] = {}

    def test_reports_new_version(self):
        updater._get = lambda url, **kw: (
            '__version__ = "9.9.9"' if "raw" in url else "[]"
        )
        data = updater.check("0.2.0", force=True)
        self.assertTrue(data["checked"])
        self.assertTrue(data["has_update"])
        self.assertEqual(data["latest"], "9.9.9")

    def test_same_version_is_not_an_update(self):
        updater._get = lambda url, **kw: (
            '__version__ = "0.2.0"' if "raw" in url else "[]"
        )
        data = updater.check("0.2.0", force=True)
        self.assertFalse(data["has_update"])

    def test_network_failure_is_reported_not_raised(self):
        def boom(url, **kw):
            raise TimeoutError("timed out")

        updater._get = boom
        data = updater.check("0.2.0", force=True)
        self.assertFalse(data["checked"])
        self.assertFalse(data["has_update"])
        self.assertIn("TimeoutError", data["error"])
        self.assertEqual(data["current"], "0.2.0")


if __name__ == "__main__":
    unittest.main()
