"""节目单（EPG）：拉一份公共 XMLTV，裁成我们自己的频道。

公共节目单动辄六七兆、几百个台，直接丢给播放器既慢又对不上：我们只播
三十来个台，节目单里那些本地台纯属累赘。所以这里做两件事——

1. 用 ``display-name`` 反查出我们每个频道在节目单里的 id，写进 m3u 的
   ``tvg-id``。对不上的时候播放器就没有节目单，这一步是关键。
2. 只保留这些 id 的 ``<channel>`` 和 ``<programme>``，重新生成一份
   一两兆的小节目单给播放器。

节目单是「有则有、无则无」的附加信息：上游挂了就继续用旧的，实在没有
也不影响看电视，所以全程不抛异常。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# CCTV-4 (亚洲) 这种括号里的区分信息先去掉，再统一大小写和分隔符
_PAREN = re.compile(r"\([^)]*\)")
_SEP = re.compile(r"[\s\-_·・．.]")
_CCTV = re.compile(r"^(cctv)(\d*\+?)")
_CCTV_K = re.compile(r"^(cctv)(\d+)k$")

# 频道名后缀里的画质标记，匹配节目前去掉
_SUFFIX = ("高清", "超清", "蓝光", "hd")


def key_of(name: str) -> str:
    """把频道名收敛成用于跨表比对的键。

    ``CCTV-1`` / ``CCTV1`` / ``CCTV-1 综合`` 都会变成 ``cctv1``；
    ``湖南卫视高清`` 变成 ``湖南卫视``。CGTN 各语种保留后缀，
    否则全部塌缩成同一个台。
    """
    s = (name or "").strip().lower()
    s = s.replace("（", "(").replace("）", ")")
    s = _PAREN.sub("", s)
    s = _SEP.sub("", s)
    if not s:
        return ""

    match = _CCTV_K.match(s)
    if match:  # CCTV4K / CCTV8K 是独立频道，别被当成 4 套、8 套
        return s
    match = _CCTV.match(s)
    if match:
        return match.group(1) + match.group(2)
    for suffix in _SUFFIX:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


class Epg:
    """节目单的下载、裁剪与缓存。

    ``xml()`` 给播放器要文件，``mapping()`` 给生成 m3u 时填 ``tvg-id``。
    两者都读同一份磁盘产物，进程重启后不用重新拉。
    """

    def __init__(self, data_dir: Path | str, source: str = "", ttl_hours: float = 6.0, timeout: float = 8.0):
        self.dir = Path(data_dir)
        self.out = self.dir / "epg.xml"
        self.raw = self.dir / "epg.src.xml"
        self.map_file = self.dir / "epg.map.json"
        self.source = source
        self.ttl = max(0.5, ttl_hours) * 3600
        self.timeout = timeout
        self.error = ""
        self.channels = 0
        self.built_at = 0.0
        self._lock = threading.Lock()
        self._mapping: dict[str, str] = self._load_map()

    # ---------------- 状态 ----------------
    def _load_map(self) -> dict:
        try:
            data = json.loads(self.map_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def mapping(self) -> dict:
        """频道名 → 节目单频道 id。没建过就是空字典。"""
        return dict(self._mapping)

    def stats(self) -> dict:
        age = round(time.time() - self.built_at) if self.built_at else None
        return {
            "channels": self.channels,
            "built_at": self.built_at or 0,
            "age": age,
            "error": self.error,
            "source": self.source,
        }

    def stale(self) -> bool:
        try:
            age = time.time() - self.out.stat().st_mtime
        except OSError:
            return True
        return age > self.ttl

    def clear(self) -> None:
        """丢掉已生成的节目单（换了源或者关掉功能时用）。"""
        with self._lock:
            self._mapping = {}
            self.channels = 0
            self.built_at = 0.0
            for path in (self.out, self.raw, self.map_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def xml(self, names: list[str], *, force: bool = False) -> bytes | None:
        """给播放器的节目单文件；没有就返回 None。"""
        if force or self.stale() or not self._mapping:
            self.build(names)
        try:
            return self.out.read_bytes()
        except OSError:
            return None

    # ---------------- 构建 ----------------
    def build(self, names: list[str]) -> dict:
        """重新拉取并裁剪，返回映射表。失败保留旧文件。"""
        if not self.source:
            self.error = "未配置节目单源"
            return self._mapping
        with self._lock:
            try:
                text = self._download()
            except Exception as exc:  # noqa: BLE001 - 节目单失败不该影响观看
                self.error = f"{type(exc).__name__}: {exc}"
                return self._mapping
            try:
                mapping, payload, total = _crop(text, names)
            except ET.ParseError as exc:
                self.error = f"节目单不是合法 XML：{exc}"
                return self._mapping

            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = self.out.with_name(self.out.name + ".tmp")
                tmp.write_bytes(payload)
                tmp.replace(self.out)
                self.map_file.write_text(
                    json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                self.error = f"写入节目单失败：{exc}"
                return self._mapping

            self._mapping = mapping
            self.channels = total
            self.built_at = time.time()
            self.error = ""
            return mapping

    def _download(self) -> str:
        """取原始节目单。TTL 内直接用上次下载的副本。"""
        try:
            if self.raw.exists() and time.time() - self.raw.stat().st_mtime <= self.ttl:
                return self.raw.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

        request = urllib.request.Request(self.source, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if self.raw.exists():  # 拉不到就先用旧的，别让节目单整个消失
                return self.raw.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"节目单源不可达：{exc}") from exc

        text = data.decode("utf-8", "replace")
        try:
            self.raw.write_bytes(data)
        except OSError:
            pass
        return text


def _crop(text: str, names: list[str]) -> tuple[dict, bytes, int]:
    """裁剪成只含我们频道的 XMLTV。"""
    root = ET.fromstring(text)

    # 先数一遍每个 id 有多少条节目：同一个名字可能对应多个 id，
    # 挑节目最全的那个
    hits: dict[str, int] = {}
    for prog in root.findall("programme"):
        cid = prog.get("channel") or ""
        if cid:
            hits[cid] = hits.get(cid, 0) + 1

    index: dict[str, str] = {}
    for chan in root.findall("channel"):
        cid = chan.get("id") or ""
        if not cid:
            continue
        for disp in chan.findall("display-name"):
            key = key_of(disp.text or "")
            if not key:
                continue
            best = index.get(key)
            if best is None or hits.get(cid, 0) > hits.get(best, 0):
                index[key] = cid

    mapping: dict[str, str] = {}
    order: list[str] = []
    for name in names:
        cid = index.get(key_of(name))
        if not cid:
            continue
        mapping[name] = cid
        if cid not in order:
            order.append(cid)

    wanted = set(order)
    out = ET.Element("tv", {"generator-info-name": "hd-iptv"})
    for chan in root.findall("channel"):
        if chan.get("id") in wanted:
            out.append(chan)
    for prog in root.findall("programme"):
        if prog.get("channel") in wanted:
            out.append(prog)

    body = ET.tostring(out, encoding="utf-8", xml_declaration=True)
    return mapping, body, len(order)


__all__ = ["Epg", "key_of"]
