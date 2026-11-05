"""版本检查：跟仓库里的版本号比一下，告诉面板有没有新版。

只读一个文件，不依赖 GitHub API 的额度。

**为什么要试多个地址**：``raw.githubusercontent.com`` 在国内被连接重置是常态
（报 ``Errno 104 Connection reset by peer``），只写一个地址等于「国内点了必然
报错」。这里按可达性排序依次尝试，任一个成功就用；全部失败才把每个源的具体
错误一并报出来，方便判断是线路问题还是仓库问题。
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = "lzp1314/hd-iptv"

# 按国内可达性排序：jsDelivr 有国内节点，raw 时好时坏，API 兜底。
# 第三项是该地址返回内容的形态。
_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("jsdelivr", f"https://cdn.jsdelivr.net/gh/{REPO}@main/iptvhd/__init__.py", "text"),
    ("raw", f"https://raw.githubusercontent.com/{REPO}/main/iptvhd/__init__.py", "text"),
    ("api", f"https://api.github.com/repos/{REPO}/contents/iptvhd/__init__.py?ref=main", "json-base64"),
)

CACHE_SECONDS = 600
FAIL_CACHE_SECONDS = 30
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


def _get(url: str, timeout: float = 4.0, accept: str = "application/json") -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hd-iptv-update-check", "Accept": accept}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _read_source(name: str, url: str, shape: str, now: float) -> str:
    """按源的返回形态取出版本号；取不到就抛异常，让调用方换下一个源。"""
    if shape == "json-base64":
        data = json.loads(_get(url, accept="application/vnd.github+json"))
        content = data.get("content") if isinstance(data, dict) else ""
        if not content:
            raise ValueError("返回里没有 content 字段")
        text = base64.b64decode(content).decode("utf-8", "replace")
    else:
        # 带时间戳绕开 CDN 缓存窗口，否则刚发版时会读到旧版本号
        sep = "&" if "?" in url else "?"
        text = _get(f"{url}{sep}t={int(now)}", accept="text/plain")
    version = parse_version(text)
    if not version:
        raise ValueError("没解析出版本号")
    return version


def check(current: str, *, force: bool = False) -> dict:
    """返回当前版本、最新版本、是否要更新。网络不通时如实说明。"""
    now = time.time()
    cached = _cache.get("data") or {}
    if not force and cached:
        # 成功结果缓存久一点；失败只缓存 30 秒，避免用户连点时把每个源都敲一遍
        ttl = CACHE_SECONDS if cached.get("checked") else FAIL_CACHE_SECONDS
        if now - _cache["at"] < ttl:
            return cached

    data = {
        "current": current,
        "latest": "",
        "has_update": False,
        "checked": False,
        "published_at": "",
        "url": f"https://github.com/{REPO}",
        "source": "",
        "error": "",
        "error_hint": "",
    }

    # 三个地址并发问，谁先回都行。串行试的话，被墙的前两个各卡满超时，
    # 点一下按钮要等十几秒才见到结果。
    errors: list[str] = []
    results: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=len(_SOURCES)) as pool:
        futures = {
            pool.submit(_read_source, name, url, shape, now): name
            for name, url, shape in _SOURCES
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append((name, fut.result()))
            except Exception as exc:  # noqa: BLE001 - 换下一个源
                reason = getattr(exc, "reason", None) or exc
                errors.append(f"{name}: {type(exc).__name__} {reason}")

    if results:
        # 多个源都返回了，取最大的那个：CDN 有缓存，可能还停在旧版本
        name, version = max(results, key=lambda item: as_tuple(item[1]))
        data["latest"] = version
        data["source"] = name
    else:
        data["error"] = "连不上更新服务器（" + "；".join(errors) + "）"
        data["error_hint"] = "多半是 NAS 到 GitHub 的线路被重置，不影响直播，可稍后再试"
        _cache["at"] = now
        _cache["data"] = data
        return data

    data["checked"] = True
    data["has_update"] = newer(data["latest"], current)
    _cache["at"] = now
    _cache["data"] = data
    return data


__all__ = ["check", "parse_version", "newer", "as_tuple"]
