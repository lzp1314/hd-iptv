"""线路历史成绩：拿过去几轮的投票来挑线路。

只看本轮探测有个绕不过去的毛病：探测只是**一次采样**。这条路此刻恰好能通，
就排到列表第一位；下一分钟它 403 了，播放器打开就是「无法播放」。而同一个台的
另一条线路可能一直很稳，只是这一轮恰好慢了两秒，被排到后面——而列表每个台只
写一行，排在后面的那条根本不会有人看到。

所以把每一轮的探测结果按线路攒起来：成功几次、总共几次、最近连续失败几轮。
排序时先看历史成功率，再看画质和速率。等于让过去十几轮替这一次抽样投票。

两个刻意的设计：

* **样本不足 3 轮的按「未知」处理**，排序结果与没这个模块时完全一致。新部署、
  刚加的源不会被历史拖后腿。
* **运营商官方源不记历史**。它失败多半是本机出口被限流，不是线路本身坏；把它
  记成低成功率，下一轮反而会把它排到聚合源后面——正好把最稳的那类源让出去了。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .models import HIST_FLAKY, HIST_MIN_SAMPLES, HIST_STABLE, Stream
from .settings import Settings

_FILE = "history.json"
_VERSION = 1

# 样本少于这么多轮就当作「不知道」，不参与排序。默认 24 小时一轮，3 轮=3 天。
MIN_SAMPLES = HIST_MIN_SAMPLES

# 成功率高于它算「稳」，低于它算「不稳」，中间算「一般」。门槛定义在 models，
# 因为排序要看它；这里只是转出来给调用方少 import 一个模块。
STABLE_RATE = HIST_STABLE
FLAKY_RATE = HIST_FLAKY

# 条目上限。几十个台 × 几条线路也就几百条，留 4000 是给频繁换地址的源兜底，
# 防止文件年复一年涨下去。
MAX_ENTRIES = 4000


def _path(settings: Settings) -> Path:
    return settings.data_dir / _FILE


def stream_key(stream: Stream) -> str:
    """这条线路的稳定标识。

    优先用 slug：央视频那类源每轮地址都换，只有频道标识是稳定的。静态源的
    slug 是地址本身编码出来的，也稳定。都没有才退回地址。

    取 sha1 前 16 位而不是原样存：地址带签名时能有几百个字符，几百条线路的
    文件就会涨到几百 KB，而这里只需要「能区分」。
    """
    raw = (stream.slug or stream.url or "").strip()
    if not raw:
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load(settings: Settings) -> dict:
    """读历史成绩，坏文件当作空（不能因为一个统计文件让整轮更新失败）。"""
    try:
        data = json.loads(_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": _VERSION, "lines": {}}
    if not isinstance(data, dict):
        return {"version": _VERSION, "lines": {}}
    lines = data.get("lines")
    if not isinstance(lines, dict):
        lines = {}
    return {"version": _VERSION, "lines": lines}


def save(settings: Settings, data: dict) -> None:
    """落盘。失败只静默跳过：历史成绩是锦上添花，丢了下一轮再攒。"""
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _path(settings).write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def record(settings: Settings, streams: list[Stream], data: dict | None = None) -> dict:
    """把这一轮的探测结果记进历史，返回更新后的数据。

    只记「真的探测过」的流：运营商官方源（失败多半是出口被限流）、以及没有
    地址的纯换票源（本轮压根没请求上游）都不记。
    """
    data = data if data is not None else load(settings)
    lines: dict = data.setdefault("lines", {})
    seen: set[str] = set()
    touched = 0
    for s in streams:
        if s.trusted:
            continue
        if not (s.url or s.slug):
            continue
        # 采集阶段就只登记了频道、没取地址的，这一轮没有真实结论
        if s.playlist_only and not s.url:
            continue
        key = stream_key(s)
        if not key or key in seen:
            continue
        seen.add(key)
        row = lines.get(key)
        if not isinstance(row, dict):
            row = {}
        row["all"] = int(row.get("all") or 0) + 1
        row["ok"] = int(row.get("ok") or 0) + (1 if s.ok else 0)
        row["fails"] = 0 if s.ok else int(row.get("fails") or 0) + 1
        row["at"] = int(time.time())
        lines[key] = row
        touched += 1

    pruned = 0
    if len(lines) > MAX_ENTRIES:
        # 按最近一次出现的时间淘汰最旧的。放在 touched 之外：文件被外部塞大时
        # 也要能瘦回来，不然清洗逻辑跟着「本轮有没有产生新记录」走就永远不触发。
        ordered = sorted(lines.items(), key=lambda kv: int((kv[1] or {}).get("at") or 0))
        for key, _ in ordered[: len(lines) - MAX_ENTRIES]:
            lines.pop(key, None)
            pruned += 1

    if touched or pruned:
        save(settings, data)
    return data


def apply(streams: list[Stream], data: dict) -> None:
    """把历史成绩回填到流上，供 ``Stream.sort_key`` 使用。"""
    lines = data.get("lines") or {}
    if not lines:
        return
    for s in streams:
        key = stream_key(s)
        row = lines.get(key)
        if not isinstance(row, dict):
            continue
        total = int(row.get("all") or 0)
        if total <= 0:
            continue
        s.hist_samples = total
        s.hist_fails = int(row.get("fails") or 0)
        s.hist_rate = min(1.0, max(0.0, int(row.get("ok") or 0) / total))


def rank(stream: Stream) -> int:
    """历史成绩分档：0 稳、1 一般/未知、2 不稳（逻辑在 ``Stream.hist_bucket``）。"""
    return stream.hist_bucket()


__all__ = ["load", "save", "record", "apply", "rank", "stream_key", "MIN_SAMPLES"]
