"""自定义源：用户自己填的本地列表 / rtp2http / 任意订阅。

和 ``config/sources.toml`` 里那些内置源的区别只有三点：

1. 存储独立（``config/custom_sources.json``）。面板要能增删改，而重写 toml
   会把用户手写的注释和顺序全冲掉——那是他自己的文件，不该被程序改写。
2. **判定落在每条流上**，不是每个源。一个 rtp2http 的 ``/playlist.m3u`` 里
   可能每一行都是裸 TS，也可能混着 HLS，只能逐条看。
3. 多一个 ``mode``（auto / direct / relay）给用户手动兜底：后缀谎报
   （裸流地址写成 ``.m3u8``）时自动识别会判错，这时用「强制直连」覆盖。

为什么要有 direct 这个概念：本机转发层只会中继 m3u8（``relay.playlist`` 拿到
非清单内容直接报错），裸 TS 一旦被包成 ``/live/xxx.m3u8`` 就是 100% 打不开。
所以裸流必须原地址下发，由播放器直连。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MODES = ("auto", "direct", "relay")
MODE_LABELS = {
    "auto": "自动识别",
    "direct": "强制直连",
    "relay": "强制经 NAS 转发",
}

_STORE_NAME = "custom_sources.json"
_SCHEMES = ("http://", "https://", "rtmp://", "rtsp://", "rtp://", "udp://")
MAX_SOURCES = 30
MAX_LINES = 400


@dataclass
class CustomSource:
    """一条用户自定义源。``name`` 就是订阅里的分组名。"""

    name: str
    url: str = ""
    lines: str = ""            # 逐行手填的「频道名,地址」，与 url 二选一
    ua: str = ""
    mode: str = "auto"
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        return asdict(self)


def store_path(settings) -> Path:
    """和 sources.toml 放同一个目录，备份/迁移时不会漏掉。"""
    return Path(settings.sources_file).parent / _STORE_NAME


def _clean_name(value: str) -> str:
    """分组名会进 m3u 的 group-title，换行和引号必须去掉。"""
    text = re.sub(r'[\r\n"]+', " ", str(value or "")).strip()
    return text[:40]


def coerce_mode(value) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in MODES else "auto"


def _clean_lines(value: str) -> str:
    kept = []
    for raw in str(value or "").replace("\r", "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kept.append(line)
        if len(kept) >= MAX_LINES:
            break
    return "\n".join(kept)


def normalize(item: dict, *, keep_id: str = "") -> CustomSource | None:
    """把面板/JSON 来的一项收敛成合法对象；名称或地址都空的返回 None。"""
    if not isinstance(item, dict):
        return None
    name = _clean_name(item.get("name"))
    url = str(item.get("url") or "").strip()
    lines = _clean_lines(item.get("lines"))
    if not name:
        return None
    if not url and not lines:
        return None
    if url and not url.lower().startswith(_SCHEMES):
        return None
    # 逐行模式里的地址合法性交给解析器，这里只挡明显乱填
    return CustomSource(
        name=name,
        url=url,
        lines=lines,
        ua=str(item.get("ua") or "").strip()[:200],
        mode=coerce_mode(item.get("mode")),
        enabled=bool(item.get("enabled", True)),
        id=str(item.get("id") or keep_id or uuid.uuid4().hex[:10]),
    )


def parse_payload(values: dict) -> list[CustomSource]:
    """面板提交的整份列表 → 合法对象列表。

    整份覆盖而不是逐条增删：面板本来就拿着完整列表，逐条接口只会让前端多写
    三套请求，出错时还容易出现「删了一半」的中间状态。
    """
    raw = values.get("sources")
    if not isinstance(raw, list):
        raise ValueError("需要 sources 数组")
    if len(raw) > MAX_SOURCES:
        raise ValueError(f"最多 {MAX_SOURCES} 条自定义源")
    out: list[CustomSource] = []
    seen: set[str] = set()
    for item in raw:
        obj = normalize(item)
        if obj is None:
            continue
        if obj.name in seen:
            # 同名源会并成同一个分组，没必要留两条
            continue
        seen.add(obj.name)
        out.append(obj)
    return out


def load(settings) -> list[CustomSource]:
    path = store_path(settings)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 文件坏了不能拖垮整轮更新：当作没有自定义源，面板里仍可重填覆盖
        return []
    raw = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[CustomSource] = []
    for item in raw:
        obj = normalize(item) if isinstance(item, dict) else None
        if obj is not None:
            out.append(obj)
    return out


def save(settings, items: list[CustomSource]) -> None:
    path = store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "sources": [x.to_dict() for x in items]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def as_source_dicts(items: list[CustomSource]) -> list[dict]:
    """转成 ``load_sources`` / ``build_collectors`` 认识的源条目。"""
    return [
        {
            "name": x.name,
            "type": "custom",
            "enabled": x.enabled,
            "url": x.url,
            "lines": x.lines,
            "ua": x.ua,
            "mode": x.mode,
        }
        for x in items
    ]


# --------------------------------------------------------------------------- #
# 逐条判定「直连」还是「经本机转发」
# --------------------------------------------------------------------------- #
def _looks_hls(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower().rstrip("/")
    except ValueError:
        return False
    return path.endswith((".m3u8", ".m3u"))


def wants_relay(url: str, mode: str = "auto") -> bool:
    """这条流该不该由本机取清单。

    ``auto`` 只看后缀，不额外发请求：采集阶段已经证明了「这个地址能取到」，
    再为每条流探一次会把几十个台的源变成几十个请求。后缀谎报的情况交给用户
    的「强制直连」覆盖。
    """
    mode = coerce_mode(mode)
    if mode == "direct":
        return False
    if mode == "relay":
        return True
    return _looks_hls(url)


def apply_mode(streams: list, mode: str, source_name: str, ua: str = "") -> list:
    """给采集到的一组流打上自定义源标记。"""
    for s in streams:
        s.source = source_name
        s.custom = True
        if ua:
            s.user_agent = ua
        s.direct = not wants_relay(s.url, mode)
    return streams


def classify(client, url: str, ua: str = "", mode: str = "auto") -> dict:
    """面板「测试这个地址」用：发一次请求，说清它是什么、会怎么播。

    只取前 256KB——足够分辨清单（几 KB）和裸流（开头就能看到 TS 包头）。
    """
    from .collectors import parse_list

    headers = {"User-Agent": ua} if ua else None
    try:
        resp = client.get(url, limit=262144, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 面板要的是人话，不是堆栈
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if resp.status not in (200, 206):
        return {"ok": False, "error": resp.error or f"HTTP {resp.status}"}
    if not resp.body:
        return {"ok": False, "error": "响应为空"}

    head = resp.body[:64].lstrip()
    if head.startswith(b"#EXTM3U") or b"http" in resp.body[:4096]:
        streams = parse_list(resp.text)
        if streams:
            relay = sum(1 for s in streams if wants_relay(s.url, mode))
            names = [(s.raw_name or s.url)[:24] for s in streams[:5]]
            return {
                "ok": True,
                "kind": "playlist",
                "channels": len(streams),
                "relay": relay,
                "direct": len(streams) - relay,
                "sample": names,
            }

    if resp.body[:1] == b"\x47":  # MPEG-TS 同步字节
        return {"ok": True, "kind": "raw", "channels": 1, "relay": 0, "direct": 1,
                "sample": []}
    return {"ok": True, "kind": "unknown", "channels": 1, "relay": 0, "direct": 1,
            "sample": []}
