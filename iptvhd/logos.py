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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 公共台标库。``logo.catvod.com`` 排第一：它是覆盖最全的一个，卫视之外
# 还收着湖北本地、CHC、风云系列这些地方台/付费频道（实测 39 个新台里 35 个
# 直接有图）；缺点是认 UA，不带 UA 直接 403——下面的 ``_fetch`` 统一带浏览器
# UA，所以没问题。
#
# 后面几个是 fanmingming/live 的镜像，作为兜底：它收录的台名多是不带分隔符
# 的写法（``CCTV1.png``），跟 catvod 的命名并不完全重合，两边互补。
MIRRORS = (
    "https://logo.catvod.com/{name}.png",
    "https://jsd.onmicrosoft.cn/gh/fanmingming/live@main/tv/{name}.png",
    "https://gcore.jsdelivr.net/gh/fanmingming/live@main/tv/{name}.png",
    "https://fastly.jsdelivr.net/gh/fanmingming/live@main/tv/{name}.png",
    "https://ghproxy.net/https://raw.githubusercontent.com/fanmingming/live/main/tv/{name}.png",
)

# 台名对不上图名的，直接给出正确写法。目前只有中国教育台这一组：
# 台标库里叫 ``CETV1``，用「中国教育1」去查永远是 404。
ALIASES = {
    "中国教育1": ("CETV1", "中国教育一台"),
    "中国教育2": ("CETV2", "中国教育二台"),
    "中国教育4": ("CETV4", "中国教育四台"),
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 1×1 全透明 PNG
PLACEHOLDER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]')


def candidates(name: str) -> list[str]:
    """同一个台在台标库里的几种可能写法。

    **原名排第一**：``CCTV-4K`` 必须先在 catvod 里查 ``CCTV-4K.png``，
    它是 23KB 的正经图标；一旦先去连字符变成 ``CCTV4K``，命中的是一张
    4.5KB 的小图——图是有的，但那是另一个频道的。

    后面的变体用来兜住三种真实存在的错位：
    - 运营商清单里带尾下划线（``湖北影视_``、``CCTV高尔夫网球_``），
      台标库按干净名字收录，去掉下划线才查得到；
    - fanmingming 那套库用 ``CCTV1`` 这种不带分隔符的写法；
    - 中国教育台在库里叫 ``CETV1``，走 ``ALIASES``。
    """
    raw = (name or "").strip()
    if not raw:
        return []
    out = [
        raw,                                    # 原名优先（CCTV-4K）
        raw.rstrip("_"),                        # 去尾下划线（湖北影视_）
        raw.replace("_", ""),                   # 去全部下划线
        raw.replace("-", "").replace(" ", ""),  # fanmingming 的写法（CCTV1）
        raw.replace("-", ""),
        raw.replace(" ", ""),
    ]
    key = raw.replace(" ", "")
    for alias, alts in ALIASES.items():
        if key == alias:
            out.extend(alts)
    seen: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.append(item)
    return seen


class Logos:
    """按频道名取台标，成功一次就落到本地。"""

    def __init__(self, cache_dir: Path | str, *, timeout: float = 3.5, retry_after: float = 300.0):
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

    def prefetch(self, names: list[str], workers: int = 6) -> int:
        """批量预热，更新完顺手跑一遍，播放器来要的时候就是本地文件。

        要并发：一个台标平均 1~3 秒，89 个台串行下来要三四分钟，这段时间里
        播放器每要一张图都得现场等，列表滚动就一顿一顿的。6 路并发实测能压到
        40 秒左右。

        缓存在 ``output/logos``（data_dir 在 compose 里是挂载卷），容器重建
        不会重抓。
        """
        todo = [n for n in names if not self.path_of(n).exists()]
        if not todo:
            return 0
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ok in pool.map(lambda n: self.get(n) is not PLACEHOLDER, todo):
                if ok:
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
