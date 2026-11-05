"""版本检查：跟仓库里的版本号比一下，告诉面板有没有新版。

只读一个文件，不依赖 GitHub API 的额度；API 只用来补充提交时间，
拿不到也不影响结论。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

REPO = "lzp1314/hd-iptv"
_RAW_VERSION = f"https://raw.githubusercontent.com/{REPO}/main/iptvhd/__init__.py"
_API_COMMIT = f"https://api.github.com/repos/{REPO}/commits?per_page=1"

CACHE_SECONDS = 600
_cache: dict = {"at": 0.0, "data": {}}
_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')


def parse_version(text: str) -> str:
    hit = _VERSION_RE.search(text or "")
    return hit.group(1) if hit else ""


def as_tuple(version: str) -> tuple:
    """把 1.2.3 拆成可比较的元组，非数字部分按 0 处理。"""
    parts = []
    for chunk in (version or "").split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts) or (0,)


def newer(latest: str, current: str) -> bool:
    return bool(latest) and as_tuple(latest) > as_tuple(current)


def _get(url: str, timeout: float = 6.0, accept: str = "application/json") -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hd-iptv-update-check", "Accept": accept}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def check(current: str, *, force: bool = False) -> dict:
    """返回当前版本、最新版本、是否要更新。网络不通时如实说明。"""
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]

    data = {
        "current": current,
        "latest": "",
        "has_update": False,
        "checked": False,
        "published_at": "",
        "url": f"https://github.com/{REPO}",
        "error": "",
    }

    try:
        # 带上时间戳绕开 raw 的 5 分钟 CDN 缓存，否则刚发版时查不到新版
        data["latest"] = parse_version(
            _get(f"{_RAW_VERSION}?t={int(now)}", accept="text/plain")
        )
        data["checked"] = bool(data["latest"])
    except Exception as exc:  # noqa: BLE001 - 检查更新失败不该影响主功能
        data["error"] = f"{type(exc).__name__}: {exc}"
        return data

    data["has_update"] = newer(data["latest"], current)

    try:
        commits = json.loads(_get(_API_COMMIT))
        if isinstance(commits, list) and commits:
            data["published_at"] = commits[0].get("commit", {}).get("committer", {}).get("date", "")
    except Exception:  # noqa: BLE001 - 只是补充信息，失败就算了
        pass

    _cache["at"] = now
    _cache["data"] = data
    return data


__all__ = ["check", "parse_version", "newer", "as_tuple"]
