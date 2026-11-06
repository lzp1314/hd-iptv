"""台标：本机代理 + 磁盘缓存。

播放器里的 ``tvg-logo`` 如果直接指向公共图床，会遇到两个问题：图床被墙
或者防盗链，台标就全空白。所以 m3u 里写的是 ``/logo/<频道名>.png``，
由服务端去取一次、存到本地，之后一直用本地这份。

取不到就回一张 1×1 透明 PNG——播放器不会因为图裂弹错，也不影响播放。
"""

from __future__ import annotations

import base64
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 公共台标库（fanmingming/live）的几个镜像。国内 jsDelivr 的镜像通常能通，
# 都不通就退回占位图，不阻塞播放。
MIRRORS = (
    "https://jsd.onmicrosoft.cn/gh/fanmingming/live@main/tv/{name}.png",
    "https://gcore.jsdelivr.net/gh/fanmingming/live@main/tv/{name}.png",
    "https://fastly.jsdelivr.net/gh/fanmingming/live@main/tv/{name}.png",
    "https://ghproxy.net/https://raw.githubusercontent.com/fanmingming/live/main/tv/{name}.png",
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 1×1 全透明 PNG
PLACEHOLDER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]')


def candidates(name: str) -> list[str]:
    """同一个台在台标库里的几种可能写法。

    台标库的文件名基本不带分隔符（``CCTV1.png``、``CCTV5+.png``），所以
    去掉 ``-`` 和空格的写法排前面——先在最快的那次尝试里命中，别为了一个
    不存在的文件名把四个镜像都试一遍。
    """
    raw = (name or "").strip()
    if not raw:
        return []
    out = [raw.replace("-", "").replace(" ", ""), raw.replace("-", ""), raw.replace(" ", ""), raw]
    seen: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.append(item)
    return seen


class Logos:
    """按频道名取台标，成功一次就落到本地。"""

    def __init__(self, cache_dir: Path | str, *, timeout: float = 2.5, retry_after: float = 1800.0):
        self.dir = Path(cache_dir)
        self.timeout = timeout
        self.retry_after = retry_after
        self._miss: dict[str, float] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def path_of(self, name: str) -> Path:
        safe = _BAD_CHARS.sub("_", (name or "").strip())[:80] or "unknown"
        return self.dir / f"{safe}.png"

    def get(self, name: str) -> bytes:
        """拿台标字节。本地有就直接给，没有就现取，再不行给占位图。"""
        path = self.path_of(name)
        try:
            data = path.read_bytes()
            if data.startswith(PNG_MAGIC):
                self.hits += 1
                return data
        except OSError:
            pass

        with self._lock:
            if time.time() < self._miss.get(name, 0.0):
                self.misses += 1
                return PLACEHOLDER

        data = self._fetch(name)
        if data is None:
            with self._lock:
                self._miss[name] = time.time() + self.retry_after
            self.misses += 1
            return PLACEHOLDER

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
        self.hits += 1
        return data

    def prefetch(self, names: list[str]) -> int:
        """批量预热，更新完顺手跑一遍，播放器来要的时候就是本地文件。"""
        done = 0
        for name in names:
            if self.path_of(name).exists():
                continue
            if self.get(name) is not PLACEHOLDER:
                done += 1
        return done

    def _fetch(self, name: str) -> bytes | None:
        for label in candidates(name):
            quoted = urllib.parse.quote(label, safe="")
            for template in MIRRORS:
                url = template.format(name=quoted)
                request = urllib.request.Request(url, headers={"User-Agent": UA})
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                        data = resp.read()
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    continue
                if data.startswith(PNG_MAGIC):
                    return data
        return None

    def stats(self) -> dict:
        files = 0
        try:
            files = sum(1 for _ in self.dir.glob("*.png"))
        except OSError:
            pass
        return {"cached": files, "hits": self.hits, "misses": self.misses}


__all__ = ["Logos", "PLACEHOLDER", "candidates"]
