"""咪咕视频：按公开接口约定现场换票的频道解析模块。

## 它是怎么工作的

平台把「频道」和「当前有效的播放地址」分开：频道有一个长期不变的数字编号，
播放地址则是点播那一刻用「编号 + 时间戳 + 校验串」现场换来的，时效很短。
所以列表里只存编号，播放时再换票——这与本项目的央视频解析器是同一套思路。

  1. 频道表 —— 公开的分类接口给出「频道名 → 编号」，不含播放地址；
  2. 现场换票 —— 用编号、时间戳和按公开约定算出的校验串请求播放接口，
     返回一段带时效的调度地址；
  3. 调度地址 —— 播放器请求它时，平台按请求方网络就近返回一个边缘节点。
     **这一步必须由播放器自己发起**：服务端代取会把服务端所在网络的节点
     写进清单，外省用户拿到后就是跨网取流，卡顿。

## 画质与账号

同一频道按档位换票，游客能拿到标清，绑定账号后按账号权益给高清及以上。
账号只在本机参与换票请求，绝不写进播放地址、日志或订阅列表。
平台对过期账号不报错、只静默按游客给流，所以换票回包里的登录状态要检查，
配了账号却被当游客时明确提示，而不是悄悄掉画质。

## 边界（实测，2026-11）

- 同一出口短时间高频换票会被拒绝，所以所有请求串行、带节拍和冷却；
- 请求某个档位超出账号权益时，回包会给出平台愿意给的档位，按它降档重取；
- 少数频道返回的调度地址没有校验串字段，直接用即可；有的必须补齐，否则被拒。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .net import HttpClient

# --------------------------------------------------------------------------- #
# 协议常量
# --------------------------------------------------------------------------- #
_CATES_URL = "https://program-sc.miguvideo.com/live/v2/tv-data/{cate}"
# 分类：去掉「热门」（与别的分类重复）
_CATE_IDS = (
    "7538163cdac044398cb292ecf75db4e0",  # 体育
    "1ff892f2b5ab4a79be6e25b69d2f5d05",  # 央视
    "0847b3f6c08a4ca28f85ba5701268424",  # 卫视
    "855e9adc91b04ea18ef3f2dbd43f495b",  # 地方
    "10b0d04cb23d4ac5945c4bc77c7ac44e",  # 影视
    "c584f67ad63f4bc983c31de3a9be977c",  # 新闻
    "af72267483d94275995a4498b2799ecd",  # 教育
    "e76e56e88fff4c11b0168f55e826445d",  # 熊猫
    "192a12edfef04b5eb616b878f031f32f",  # 综艺
    "fc2f5b8fd7db43ff88c4243e731ecede",  # 少儿
    "e1165138bdaa44b9a3138d74af6c6673",  # 纪实
)

_PLAY_URL = "https://play.miguvideo.com/playurl/v1/play/playurl"
_APP_VERSION = "26000370"
_SIGN_SUFFIX = "3ce941cc3cbc40528bfd1c64f9fdf6c0migu0123"
_SALT = 1230024
_DD_KEYS = "cdabyzwxkl"

# 档位 → (面板文案, 高度)。档位号是平台的约定，不是码率。
RATES: dict[int, tuple[str, int]] = {
    2: ("标清 540P", 540),
    3: ("高清 720P", 720),
    4: ("蓝光 1080P", 1080),
    7: ("原画", 1080),
    9: ("4K 臻享", 2160),
}
DEFAULT_RATE = 2

# 需要账号才可能拿到的档位。游客请求这些档位平台也会回 200，只是静默给标清。
ACCOUNT_RATES = (3, 4, 7, 9)

UA = "android"


# --------------------------------------------------------------------------- #
# 限流：与央视频同样的节拍 + 冷却
# --------------------------------------------------------------------------- #
_pace_lock = threading.Lock()
_pace_last = 0.0
_deny_lock = threading.Lock()
_deny_streak = 0
_deny_until = 0.0
_DENY_STREAK = 8
_DENY_SECONDS = 30.0


def pace(min_gap: float = 0.35) -> None:
    """所有换票请求串行发出，间隔固定。并发会被当成异常流量。"""
    global _pace_last
    with _pace_lock:
        now = time.monotonic()
        wait = _pace_last + min_gap - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _pace_last = now


def denied() -> bool:
    return time.time() < _deny_until


def note_denied() -> None:
    global _deny_streak, _deny_until
    with _deny_lock:
        _deny_streak += 1
        if _deny_streak >= _DENY_STREAK:
            _deny_until = time.time() + _DENY_SECONDS
            _deny_streak = 0


def note_ok() -> None:
    global _deny_streak
    with _deny_lock:
        _deny_streak = 0


def reset_state() -> None:
    """测试用：清掉进程内的节拍和冷却状态。"""
    global _pace_last, _deny_streak, _deny_until
    with _pace_lock:
        _pace_last = 0.0
    with _deny_lock:
        _deny_streak = 0
        _deny_until = 0.0


# --------------------------------------------------------------------------- #
# 协议细节
# --------------------------------------------------------------------------- #
def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _dd_calcu(pu_data: str, pid: str, rate: int, user_id: str = "") -> str:
    """把平台回包里的 puData 补成可直接请求的校验串。

    按首尾交错展开，并在固定位置插入几个由日期、频道编号和账号决定的字符。
    这些位置是平台的约定，改一位就会被拒。
    """
    if not pu_data:
        return ""
    words = ["v", "a", "0", "a"]
    short_id = 3 < len(user_id) <= 8
    if short_id:
        words[0] = "e"
    elif user_id and len(user_id) > 7 and user_id[7].isdigit():
        words[0] = _DD_KEYS[int(user_id[7])]
    if rate == 2:
        words[0] = "v"

    day = datetime.now().strftime("%Y%m%d")
    pid_at = int(pid[6]) if len(pid) > 6 and pid[6].isdigit() else 0
    out: list[str] = []
    half = len(pu_data) // 2
    for i in range(half):
        out.append(pu_data[len(pu_data) - i - 1])
        out.append(pu_data[i])
        if i == 1:
            out.append(words[0])
        elif i == 2:
            out.append(_DD_KEYS[int(day[0])])
        elif i == 3:
            out.append(_DD_KEYS[pid_at])
        elif i == 4:
            out.append(words[3])
    return "".join(out)


def _seal(raw: str, pid: str, rate: int, user_id: str = "") -> str:
    """调度地址需要补校验串时补上，否则原样返回。"""
    if "&puData=" not in raw:
        return raw
    pu = raw.split("&puData=", 1)[1]
    return f"{raw}&ddCalcu={_dd_calcu(pu, pid, rate, user_id)}&sv=10004&ct=android"


def clean_account(user_id, token) -> tuple[str, str]:
    """账号只接受可放进请求头的可见 ASCII。非法字符按未登录处理。"""
    out = []
    for value in (user_id, token):
        text = str(value or "").strip()
        if text and not all("\x21" <= c <= "\x7e" for c in text):
            return "", ""
        out.append(text)
    return out[0], out[1]


def account_ok(user_id: str, token: str) -> bool:
    return bool(user_id and token and clean_account(user_id, token) == (user_id, token))


class MiguError(RuntimeError):
    """换票失败。"""


class MiguUnavailable(MiguError):
    """平台限制，重试也没用。"""


@dataclass(slots=True)
class Ticket:
    url: str          # 未跟随跳转的调度地址，交给播放器就近取流
    rate: int         # 平台实际给到的档位
    logined: bool     # 这次换票是否认了账号
    tried: int        # 实际请求的档位（可能已降档）


def _fail_text(data: dict | None) -> str:
    if not data:
        return "接口没有响应（网络不可达，或出口 IP 被平台拒绝）"
    msg = str(data.get("message") or data.get("desc") or "")
    if msg and msg != "SUCCESS":
        return msg
    rid = str(data.get("rid") or "")
    return f"接口返回异常（{rid}）" if rid else "接口返回异常"


def ticket(
    pid: str,
    rate: int = DEFAULT_RATE,
    *,
    user_id: str = "",
    token: str = "",
    h265: bool = True,
    hdr: bool = False,
    client: HttpClient | None = None,
    attempts: int = 2,
    pause: float = 1.0,
) -> Ticket:
    """按档位换一张当前有效的调度地址。

    返回的地址**没有请求过**，交给播放器后由平台按播放器网络就近调度。
    """
    user_id, token = clean_account(user_id, token)
    rate = int(rate) if int(rate) in RATES else DEFAULT_RATE
    client = client or HttpClient(timeout=12, retries=1)
    last = "换票失败"

    for round_no in range(max(1, attempts)):
        if denied():
            raise MiguError("平台限流中，稍后自动恢复")

        ts = str(round(time.time() * 1000))
        sign = _md5(_md5(ts + pid + _APP_VERSION) + _SIGN_SUFFIX)
        headers = {
            "AppVersion": 2600037000,
            "TerminalId": "android",
            "X-UP-CLIENT-CHANNEL-ID": "2600037000-99000-200300220100002",
            "Accept": "application/json",
        }
        # 两个赛事频道开了 flv 就没有回放，按平台约定不带这个头
        if pid not in ("641886683", "641886773"):
            headers["appCode"] = "miguvideo_default_android"
        # 标清一律按游客请求，这样回包里的登录状态才有判假的意义
        sends_account = rate != 2 and bool(user_id and token)
        if sends_account:
            headers["UserId"] = user_id
            headers["UserToken"] = token

        extra = ("&h265N=true" if h265 else "") + (
            "&4kvivid=true&2Kvivid=true&vivid=2" if hdr else ""
        )
        url = (
            f"{_PLAY_URL}?sign={sign}&rateType={rate}&contId={pid}"
            f"&timestamp={ts}&salt={_SALT}&flvEnable=true&super4k=true{extra}"
        )

        pace()
        resp = client.get(url, headers=headers)
        if resp.status in (403, 429):
            note_denied()
        try:
            data = json.loads(resp.text) if resp.body else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            last = f"接口无响应（{resp.error or resp.status}）"
            time.sleep(pause)
            continue

        rid = str(data.get("rid") or "")
        info = ((data.get("body") or {}).get("urlInfo") or {})
        raw = str(info.get("url") or "")

        # 请求的档位超出权益：平台在回包里说出愿意给的档位，按它降一次
        if rid == "TIPS_NEED_MEMBER":
            offered = int(info.get("rateType") or 0)
            fallback = 4 if offered >= 4 else 3
            if fallback != rate:
                rate = fallback
                last = f"账号权益不足，已降到 {RATES.get(fallback, (str(fallback),))[0]}"
                time.sleep(pause)
                continue
            raise MiguError("账号权益不足：当前档位不可用")

        if rid != "SUCCESS" or not raw:
            last = _fail_text(data)
            if rid in ("TIPS_NO_RIGHT", "TIPS_NOT_ONLINE", "TIPS_PLAY_LIMIT"):
                raise MiguUnavailable(last)
            note_denied()
            time.sleep(pause)
            continue

        note_ok()
        got = int(info.get("rateType") or rate)
        logined = bool(((data.get("body") or {}).get("auth") or {}).get("logined"))
        return Ticket(url=_seal(raw, pid, rate, user_id), rate=got,
                      logined=logined, tried=rate)

    raise MiguError(last)


# --------------------------------------------------------------------------- #
# 频道表
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ChannelRow:
    name: str
    pid: str
    group: str
    logo: str = ""


_CHANNEL_CACHE = "migu-channels.json"
# 进程内的 pID → 展示名。采集时填好，播放时给错误提示用；重启后由缓存重建。
_NAMES: dict[str, str] = {}


def name_of(pid: str, data_dir: Path | None = None) -> str:
    """按 pID 找展示名，只用于播放时给错误提示，读不到也不影响换票。"""
    name = _NAMES.get(str(pid), "")
    if name or data_dir is None:
        return name
    try:
        payload = json.loads((data_dir / _CHANNEL_CACHE).read_text(encoding="utf-8"))
        for row in payload.get("channels") or []:
            if str(row.get("pid")) == str(pid):
                _NAMES[str(pid)] = str(row.get("name") or "")
                return _NAMES[str(pid)]
    except (OSError, ValueError, TypeError):
        pass
    return ""


def fetch_channels(client: HttpClient, *, timeout_minutes: float = 360.0,
                   data_dir: Path | None = None) -> list[ChannelRow]:
    """抓频道表，按刷新间隔复用本地缓存。

    采集阶段只登记「名字 → 编号」，不换票、不发账号，避免一轮更新就打几十次
    播放接口被限流。缓存只存这份公开目录，不存任何播放地址或账号信息。
    """
    cache = (data_dir / _CHANNEL_CACHE) if data_dir else None
    if cache is not None:
        try:
            age = time.time() - cache.stat().st_mtime
            payload = json.loads(cache.read_text(encoding="utf-8"))
            rows = payload.get("channels") or []
            if rows and age < max(1.0, timeout_minutes) * 60:
                parsed = [ChannelRow(**r) for r in rows if r.get("name") and r.get("pid")]
                for r in parsed:
                    _NAMES[r.pid] = r.name
                return parsed
        except (OSError, ValueError, TypeError):
            pass

    rows: list[ChannelRow] = []
    seen: set[str] = set()
    failed = 0
    _NAMES.clear()
    for cate in _CATE_IDS:
        try:
            resp = client.get(_CATES_URL.format(cate=cate))
            data = json.loads(resp.text) if resp.body else {}
            items = ((data.get("body") or {}).get("dataList") or [])
        except (ValueError, Exception):  # noqa: BLE001 - 单个分类失败不影响其它
            failed += 1
            continue
        if not items:
            failed += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            pid = str(item.get("pID") or "").strip()
            if not name or not pid.isdigit() or pid in seen:
                continue
            seen.add(pid)
            _NAMES[pid] = name
            pics = item.get("pics") or {}
            logo = str(pics.get("lowResolutionH") or pics.get("lowResolutionV") or "")
            rows.append(ChannelRow(name=name, pid=pid,
                                   group=str(item.get("group") or ""), logo=logo))

    if not rows:
        raise MiguError(f"频道表获取失败（{failed}/{len(_CATE_IDS)} 个分类无响应）")

    # 按分组顺序输出，保证每次生成的列表顺序稳定
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(cache.name + ".tmp")
            payload = {
                "channels": [
                    {"name": r.name, "pid": r.pid, "group": r.group, "logo": r.logo}
                    for r in rows
                ]
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache)
        except OSError:
            pass
    return rows
