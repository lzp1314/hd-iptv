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

# 游客高清走另一套「旧版」签名：新版签名(appVersion 26000370)请求 rateType=3
# 时平台会静默按游客给标清；旧版(appVersion 2600034600)才会给真正的 720P。
_APP_VERSION_HD = "2600034600"
_CHANNEL_ID_HD = "2600034600-99000-201600010010028"
_SIGN_SUFFIX_HD = "2cac4f2c6c3346a5b34e085725ef7e33migu"

# 边缘节点解析：只请求调度地址读一次 302，带有限重试。
_EDGE_ATTEMPTS = 6
_EDGE_PAUSE = 0.15

# 档位 → (面板文案, 高度)。档位号是平台的约定，不是码率。
RATES: dict[int, tuple[str, int]] = {
    2: ("标清 540P", 540),
    3: ("高清 720P", 720),
    4: ("蓝光 1080P", 1080),
    7: ("原画", 1080),
    9: ("4K 臻享", 2160),
}
DEFAULT_RATE = 3

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


def _weave(pu_data: str, inject: dict[int, str]) -> str:
    """把 puData 首尾成对交错展开，并在约定序号后插入校验字符。

    ``inject`` 的键是展开序号（从 0 起），值是该处要插入的字符；没有对应键就跳过。
    插入位是平台校验的骨架，错一位整条地址就作废。把"位置→字符"做成一张表，
    新版票和旧版票就只需各提供自己的表，不必各写一遍展开循环。
    """
    out: list[str] = []
    for i in range(len(pu_data) // 2):
        out.append(pu_data[-1 - i])
        out.append(pu_data[i])
        char = inject.get(i)
        if char is not None:
            out.append(char)
    return "".join(out)


def _digit_at(text: str, position: int) -> int:
    """取字符串指定位置的十进制数字，越界或非数字按 0 处理。"""
    return int(text[position]) if len(text) > position and text[position].isdigit() else 0


def _lead_char(user_id: str, rate: int) -> str:
    """新版校验串第 1 个插入位：标清看档位，其余按账号尾号推导。"""
    if rate == 2:
        return "v"
    if 3 < len(user_id) <= 8:
        return "e"
    if len(user_id) > 7 and user_id[7].isdigit():
        return _DD_KEYS[int(user_id[7])]
    return "v"


def _dd_calcu(pu_data: str, pid: str, rate: int, user_id: str = "") -> str:
    """为新版换票生成校验串：日期、频道和账号各贡献一位，按约定落位。"""
    if not pu_data:
        return ""
    day = datetime.now().strftime("%Y%m%d")
    return _weave(pu_data, {
        1: _lead_char(user_id, rate),
        2: _DD_KEYS[_digit_at(day, 0)],
        3: _DD_KEYS[_digit_at(pid, 6)],
        4: "a",
    })


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
    # 游客要高清：新版签名会被平台静默降到标清，走旧版签名才真给 720P。
    # 有账号的仍按账号权益走下面的新版签名，画质跟着账号档位走。
    if rate >= 3 and not account_ok(user_id, token):
        return ticket_guest_hd(pid, h265=h265, hdr=hdr, client=client,
                               attempts=attempts, pause=pause)
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


def _dd_calcu_hd(pu_data: str, pid: str) -> str:
    """为游客高清票生成校验串：旧版约定，日期取第 3 位、首尾位固定。

    与新版共用 ``_weave`` 展开，只把注入表换成旧版那套：不认账号、不随档位变，
    日期从 ``YYYYMMDD`` 的第 3 位取（新版取第 1 位）。
    """
    if not pu_data or not pid:
        return ""
    day = datetime.now().strftime("%Y%m%d")
    return _weave(pu_data, {
        1: "v",
        2: _DD_KEYS[_digit_at(day, 2)],
        3: _DD_KEYS[_digit_at(pid, 6)],
        4: "a",
    })


def _seal_hd(raw: str, pid: str) -> str:
    """给游客高清的调度地址补旧版校验串。"""
    if "&puData=" not in raw:
        return raw
    pu = raw.split("&puData=", 1)[1]
    return f"{raw}&ddCalcu={_dd_calcu_hd(pu, pid)}&sv=10004&ct=android"


def ticket_guest_hd(
    pid: str,
    *,
    h265: bool = False,
    hdr: bool = False,
    client: HttpClient | None = None,
    attempts: int = 2,
    pause: float = 1.0,
) -> Ticket:
    """游客换高清票：用旧版 appVersion 签名，平台才会真给 720P。

    新版签名请求 rateType=3 的未登录票，平台会回 SUCCESS 但静默降到 540P；
    旧版 ``appVersion=2600034600`` 这套才会给真正的 720P（游客上限）。
    仅在没有账号、且面板选了高清及以上时走这条路；有账号的仍走新版按权益给。
    """
    client = client or HttpClient(timeout=12, retries=1)
    last = "换票失败"
    for _ in range(max(1, attempts)):
        if denied():
            raise MiguError("平台限流中，稍后自动恢复")

        ts = str(round(time.time() * 1000))
        md5 = _md5(ts + pid + _APP_VERSION_HD[:8])
        salt = f"{random.randint(0, 999999):06d}25"
        sign = _md5(md5 + _SIGN_SUFFIX_HD + salt[:4])
        headers = {
            "AppVersion": _APP_VERSION_HD,
            "TerminalId": "android",
            "X-UP-CLIENT-CHANNEL-ID": _CHANNEL_ID_HD,
        }
        if pid not in ("641886683", "641886773"):
            headers["appCode"] = "miguvideo_default_android"
        extra = ("&h265N=true" if h265 else "") + (
            "&4kvivid=true&2Kvivid=true&vivid=2" if hdr else ""
        )
        url = (
            f"{_PLAY_URL}?sign={sign}&rateType=3&contId={pid}"
            f"&timestamp={ts}&salt={salt}&flvEnable=true&super4k=true{extra}"
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
        if rid != "SUCCESS" or not raw:
            last = _fail_text(data)
            if rid in ("TIPS_NO_RIGHT", "TIPS_NOT_ONLINE", "TIPS_PLAY_LIMIT"):
                raise MiguUnavailable(last)
            note_denied()
            time.sleep(pause)
            continue

        note_ok()
        got = int(info.get("rateType") or 3)
        return Ticket(url=_seal_hd(raw, pid), rate=got, logined=False, tried=3)

    raise MiguError(last)


def edge_location(
    dispatch_url: str,
    *,
    client: HttpClient | None = None,
    attempts: int = _EDGE_ATTEMPTS,
    pause: float = _EDGE_PAUSE,
) -> str:
    """把调度地址解析成真实 CDN 节点地址：只请求调度域名读一次 302 的 Location。

    这是「服务端解析一次、播放器直连真实节点」那条路的核心。关键点在于服务端
    只碰调度域名（这一步 NAS 出口不会被平台回 661），读到的 Location 是一台真实
    边缘节点；把它交给播放器直连，清单和分片都由播放器用自己网络取，绕开 NAS
    出口被节点拒的问题，也免去了播放器自己跟多层 302 的麻烦（VLC 的弱项）。

    拿不到可用 Location 时返回空串，调用方据此回落到原来的 relay 代取。
    """
    if not dispatch_url:
        return ""
    client = client or HttpClient(timeout=6, retries=1)
    for i in range(max(1, attempts)):
        try:
            resp = client.get(dispatch_url, allow_redirect=False)
        except Exception:  # noqa: BLE001 - 网络异常繁多，重试即可
            resp = None
        if resp is not None:
            loc = str((resp.headers or {}).get("location") or "").strip()
            if resp.status in (301, 302, 303, 307, 308) and loc:
                # 平台偶尔先回一个占位的 bofang 地址，不是真节点，继续试
                if not loc.lower().startswith("http://bofang"):
                    return loc
        if i + 1 < attempts and pause > 0:
            time.sleep(pause)
    return ""


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
