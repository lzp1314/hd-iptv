"""采集器：把不同来源的数据统一成 Stream 列表。

四种内置类型：
  subscription —— m3u / txt 订阅地址（最常用）
  local        —— 本地文件或目录
  api          —— 通用 JSON 接口，字段映射由配置描述
  webpage      —— 通用网页正则抽取
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

from .hls import is_playlist
from .models import Stream
from .net import HttpClient

_ATTR = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_m3u(text: str) -> list[Stream]:
    """解析 m3u/m3u8 播放列表。"""
    streams: list[Stream] = []
    pending: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            meta = dict(_ATTR.findall(line))
            name = line.split(",", 1)[1].strip() if "," in line else ""
            pending = {
                "name": (meta.get("tvg-name") or name).strip(),
                "tvg_id": meta.get("tvg-id", ""),
                "logo": meta.get("tvg-logo", ""),
                "group": meta.get("group-title", ""),
            }
        elif line.startswith("#"):
            continue
        elif pending is not None:
            streams.append(
                Stream(
                    url=line,
                    raw_name=pending["name"],
                    tvg_id=pending["tvg_id"],
                    logo=pending["logo"],
                    group=pending["group"],
                )
            )
            pending = None
    return streams


def parse_txt(text: str) -> list[Stream]:
    """解析 txt 列表，支持 `分组,#genre#` 与 `频道名,地址` 两种行。"""
    streams: list[Stream] = []
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#genre#" in line:
            group = line.split(",", 1)[0].strip()
            continue
        if line.startswith(("http://", "https://", "rtmp://", "rtsp://")):
            streams.append(Stream(url=line, raw_name="", group=group))
            continue
        if "," not in line:
            continue
        name, _, url = line.partition(",")
        url = url.strip()
        if not url.startswith(("http://", "https://", "rtmp://", "rtsp://")):
            continue
        streams.append(Stream(url=url, raw_name=name.strip(), group=group))
    return streams


def parse_list(text: str) -> list[Stream]:
    """自动识别 m3u 还是 txt。"""
    if is_playlist(text):
        return parse_m3u(text)
    return parse_txt(text)


# --------------------------------------------------------------------------- #
# 采集器
# --------------------------------------------------------------------------- #
class Collector:
    kind = "base"

    def __init__(self, name: str = "", enabled: bool = True, config: dict | None = None) -> None:
        self.name = name or self.kind
        self.enabled = enabled
        self.config = config or {}

    def collect(self, client: HttpClient) -> list[Stream]:  # pragma: no cover - 接口
        raise NotImplementedError

    def _tag(self, streams: list[Stream]) -> list[Stream]:
        for s in streams:
            s.source = self.name
        return streams


class SubscriptionCollector(Collector):
    kind = "subscription"

    def collect(self, client: HttpClient) -> list[Stream]:
        url = self.config.get("url", "")
        if not url:
            return []
        ua = self.config.get("ua", "")
        headers = dict(self.config.get("headers") or {})
        if ua:
            headers["User-Agent"] = ua
        resp = client.get(url, headers=headers or None)
        if not resp.body:
            raise RuntimeError(f"订阅获取失败: {resp.error or resp.status}")
        return self._tag(parse_list(resp.text))


class LocalCollector(Collector):
    kind = "local"

    def collect(self, client: HttpClient) -> list[Stream]:
        path = Path(self.config.get("path", ""))
        if not path.is_absolute():
            path = Path.cwd() / path
        files: list[Path] = []
        if path.is_dir():
            files = sorted(p for p in path.iterdir() if p.suffix.lower() in {".m3u", ".m3u8", ".txt"})
        elif path.exists():
            files = [path]
        streams: list[Stream] = []
        for f in files:
            streams.extend(parse_list(f.read_text(encoding="utf-8", errors="ignore")))
        return self._tag(streams)


class ApiCollector(Collector):
    kind = "api"

    def collect(self, client: HttpClient) -> list[Stream]:
        url = self.config.get("url", "")
        if not url:
            return []
        headers = dict(self.config.get("headers") or {})
        resp = client.get(url, headers=headers or None)
        if not resp.body:
            raise RuntimeError(f"接口请求失败: {resp.error or resp.status}")
        payload = json.loads(resp.text)
        for key in str(self.config.get("json_path", "")).split("."):
            if not key:
                continue
            if "[" in key and key.endswith("]"):
                name, _, idx = key.partition("[")
                payload = payload.get(name, []) if isinstance(payload, dict) else payload
                payload = payload[int(idx.rstrip("]"))]
            elif isinstance(payload, dict):
                payload = payload.get(key, [])
        if not isinstance(payload, list):
            return []

        url_tpl = str(self.config.get("url_template", "{url}"))
        name_field = str(self.config.get("name_field", "name"))
        url_field = str(self.config.get("url_field", "url"))
        group_field = str(self.config.get("group_field", "group"))
        streams: list[Stream] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get(url_field, ""))
            if not raw_url:
                continue
            url_value = url_tpl.format(url=raw_url, **{k: v for k, v in item.items() if isinstance(v, (str, int, float))})
            streams.append(
                Stream(
                    url=url_value,
                    raw_name=str(item.get(name_field, "")),
                    group=str(item.get(group_field, "") or ""),
                )
            )
        return self._tag(streams)


class WebpageCollector(Collector):
    kind = "webpage"

    def collect(self, client: HttpClient) -> list[Stream]:
        url = self.config.get("url", "")
        pattern = self.config.get("regex", "")
        if not url or not pattern:
            return []
        headers = dict(self.config.get("headers") or {})
        resp = client.get(url, headers=headers or None)
        if not resp.body:
            raise RuntimeError(f"页面请求失败: {resp.error or resp.status}")
        try:
            name_idx = int(self.config.get("name_group", 1))
            url_idx = int(self.config.get("url_group", 2))
        except (TypeError, ValueError):
            name_idx, url_idx = 1, 2
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise RuntimeError(f"正则无效: {exc}") from exc

        base = str(self.config.get("base_url", "") or url)
        streams: list[Stream] = []
        for match in rx.finditer(resp.text):
            groups = match.groups()
            try:
                raw_url = groups[url_idx - 1]
                name = groups[name_idx - 1] if name_idx > 0 and len(groups) >= name_idx else ""
            except IndexError:
                continue
            if not raw_url:
                continue
            streams.append(
                Stream(
                    url=urljoin(base, raw_url.strip()),
                    raw_name=(name or "").strip(),
                    group=str(self.config.get("group", "") or ""),
                )
            )
        return self._tag(streams)


_REGISTRY = {
    SubscriptionCollector.kind: SubscriptionCollector,
    LocalCollector.kind: LocalCollector,
    ApiCollector.kind: ApiCollector,
    WebpageCollector.kind: WebpageCollector,
}


def _full_registry() -> dict:
    """采集器 + 解析器。

    generators 依赖本模块的 Collector，所以放在函数里延迟导入，避免循环依赖。
    """
    registry = dict(_REGISTRY)
    try:
        from .generators import GENERATORS
    except Exception:  # noqa: BLE001 - 解析器不可用不应拖垮基础采集
        return registry
    registry.update(GENERATORS)
    return registry


def build_collectors(sources: list[dict]) -> list[Collector]:
    """按配置构建采集器列表，忽略 disabled 项和未知类型。"""
    collectors: list[Collector] = []
    registry = _full_registry()
    for item in sources:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        kind = str(item.get("type", "")).strip().lower()
        cls = registry.get(kind)
        if cls is None:
            continue
        collectors.append(
            cls(
                name=str(item.get("name") or kind),
                enabled=True,
                config=item,
            )
        )
    return collectors
