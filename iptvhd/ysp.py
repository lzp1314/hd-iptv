"""央视频（yangshipin）取流协议。

## 它是怎么工作的

央视频的播放地址不放在网页里，也不在任何公开接口的明文返回中，而是由客户端
带着一个**自行构造并加密的身份凭据**去换。整个链路两跳：

  1. 造 cKey —— 把「设备指纹 + 时间戳 + 频道号 + 校验串」打包成二进制，
     过一遍 TEA-CBC 加密，再与固定字节序列做异或，最后 base64url 编码。
  2. 拿 cKey 换地址 —— GET ``bkliveinfo.ysp.cctv.cn``，返回 JSON，其中
     ``playurl`` 就是带 token 的直播地址，``backurl_list`` 是备用线路。

第 1 步是全部难度所在：TEA 加密本身不难，难的是那个二进制包的字段顺序和
两段固定的密钥/掩码。这些值是从官方客户端行为中观察得到的，不是文档。

## 几个实测得到的结论（2026-11）

- ``defn`` 决定码率档：``fhd`` 1080P / ``shd`` 720P / ``hd`` 540P。
  返回里的 ``formats`` 会列出该频道实际提供的档位。
- 返回的 ``playurl`` 形如 ``https://{cdn}/<长token>/<cnlid>.m3u8``，
  **路径里的长 token 就是取分片的目录前缀**，分片必须拼成
  ``…/<长token>/<分片名>``。用普通的相对路径解析（urljoin）会把 token
  丢掉，结果是一律 403 —— 这是接入时最容易踩的坑。
- 分片请求不需要特殊请求头，UA 给 ``qqlive`` 即可。但**短时间高频请求会被
  拒绝（403）**，等一两分钟自动恢复。正常播放节奏（每 5 秒一片）不会触发。
- 部分频道会被官方限流为「应版权方要求，暂停提供直播信号」（iretcode=12），
  这是平台授权问题，不是取源失败，无法绕过。

## 边界

地址有时效（返回里 ``vkey_renew_interval`` 给的是 4 小时），所以产物需要
定时刷新。接口或密钥一改，这里就要跟着改——这是这类取源方式的固有成本。
"""

from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit, urlunsplit

from .net import HttpClient

# --------------------------------------------------------------------------- #
# 协议常量
# --------------------------------------------------------------------------- #
_PLATFORM = 4330403
_APP_VERSION = "V8.22.1035.3031"
_INFO_URL = "https://bkliveinfo.ysp.cctv.cn/"

_TEA_KEY = bytes.fromhex("59b2f7cf725ef43c34fdd7c123411ed3")
_GUARD_KEY = bytes.fromhex("110DBEC10C23E7D2E56A1CAD6914EF1B")
_XOR_KEY = bytes([132, 46, 237, 8, 240, 102, 230, 234, 72, 180, 202, 169, 145, 237, 111, 243])
_GUARD_XOR = bytes([179, 201, 83, 160, 105, 19, 173, 77])

# 期望的编码能力声明，原样照抄客户端，改了会被判为异常设备
_VCODEC_CAP = base64.b64encode(b"H(30:1080,60:1080|30:1080,60:1080)").decode()

UA = "qqlive"
REFERER = "https://live.cctv.cn/"

# 取清单和分片时带上的头。官方 CDN 对缺 Referer 的请求更容易回 403，
# 这和播放器自己取流时带的一致。
HLS_HEADERS = {
    "Accept": "application/vnd.apple.mpegurl,application/json,*/*",
    "Referer": REFERER,
}

# 一次分片采样读多少字节（用于测速和读 SPS 里的真实分辨率）
SAMPLE_BYTES = 262144

# 码率档位 → 高度，用于给探测阶段提供分辨率提示
DEFN_HEIGHT = {"fhd": 1080, "shd": 720, "hd": 540}

# (slug, 显示名, cnlid, livepid, 档位)
CHANNELS: tuple[tuple[str, str, str, str, str], ...] = (
    ("cctv1", "CCTV-1 综合", "2024078201", "600001859", "fhd"),
    ("cctv2", "CCTV-2 财经", "2024075401", "600001800", "fhd"),
    ("cctv3", "CCTV-3 综艺", "2024068501", "600001801", "fhd"),
    ("cctv4", "CCTV-4 中文国际", "2029797101", "600001814", "fhd"),
    ("cctv5", "CCTV-5 体育", "2024078401", "600001818", "fhd"),
    ("cctv5p", "CCTV-5+ 体育赛事", "2024078001", "600001817", "fhd"),
    ("cctv6", "CCTV-6 电影", "2013693901", "600108442", "fhd"),
    ("cctv7", "CCTV-7 国防军事", "2024072001", "600004092", "fhd"),
    ("cctv8", "CCTV-8 电视剧", "2029793001", "600001803", "fhd"),
    ("cctv9", "CCTV-9 纪录", "2024078601", "600004078", "fhd"),
    ("cctv10", "CCTV-10 科教", "2024078701", "600001805", "fhd"),
    ("cctv11", "CCTV-11 戏曲", "2027248701", "600001806", "fhd"),
    ("cctv12", "CCTV-12 社会与法", "2027248801", "600001807", "fhd"),
    ("cctv13", "CCTV-13 新闻", "2029797201", "600001811", "fhd"),
    ("cctv14", "CCTV-14 少儿", "2027248901", "600001809", "fhd"),
    ("cctv15", "CCTV-15 音乐", "2027249001", "600001815", "fhd"),
    ("cctv16", "CCTV-16 奥林匹克", "2027249101", "600098637", "fhd"),
    ("cctv17", "CCTV-17 农业农村", "2027249401", "600001810", "fhd"),
    # 4K / 8K 频道（CCTV-4K、CCTV-8K、CCTV-16 4K）匿名接口只给 1080P，
    # 实测解出来的 SPS 全是 1920×1080，真 4K 要登录 + 会员。既然拿不到 4K，
    # 这三个台就没有存在意义，直接不取——省三次请求，也少三个播放时会失败的台。
    ("cgtn", "CGTN", "2024181701", "600014550", "fhd"),
    ("cgtnfr", "CGTN 法语", "2024181801", "600084704", "fhd"),
    ("cgtnru", "CGTN 俄语", "2024181901", "600084758", "fhd"),
    ("cgtnar", "CGTN 阿拉伯语", "2024182001", "600084782", "fhd"),
    ("cgtnes", "CGTN 西班牙语", "2024182101", "600084744", "fhd"),
    ("cgtndoc", "CGTN 纪录", "2024182301", "600084781", "fhd"),
    # 三个剧场频道只认 720P 档，报 fhd 会被回 iretcode=25「无登录信息」
    ("cctvfyjc", "CCTV 风云剧场", "2025637103", "600099658", "shd"),
    ("cctvdyjc", "CCTV 第一剧场", "2026874203", "600099655", "shd"),
    ("cctvhjjc", "CCTV 怀旧剧场", "2026874303", "600099620", "shd"),
    ("bjws", "北京卫视", "2024052703", "600002309", "fhd"),
    ("jsws", "江苏卫视", "2024171103", "600002521", "fhd"),
    ("dfws", "东方卫视", "2024054503", "600002483", "fhd"),
    ("zjws", "浙江卫视", "2024054703", "600002520", "fhd"),
    ("hnws", "湖南卫视", "2024054803", "600002475", "fhd"),
    ("hbws", "湖北卫视", "2024171203", "600002508", "fhd"),
    ("gdws", "广东卫视", "2024060903", "600002485", "fhd"),
    ("gxws", "广西卫视", "2024060703", "600002509", "fhd"),
    ("hljws", "黑龙江卫视", "2029797003", "600002498", "fhd"),
    ("hainanws", "海南卫视", "2024055603", "600002506", "fhd"),
    ("cqws", "重庆卫视", "2024061103", "600002531", "fhd"),
    ("szws", "深圳卫视", "2024061303", "600002481", "fhd"),
    ("scws", "四川卫视", "2024061403", "600002516", "fhd"),
    ("henanws", "河南卫视", "2029797303", "600002525", "fhd"),
    ("dnws", "东南卫视", "2024061503", "600002484", "fhd"),
    ("gzws", "贵州卫视", "2024061603", "600002490", "fhd"),
    ("jxws", "江西卫视", "2024061703", "600002503", "fhd"),
    ("lnws", "辽宁卫视", "2024171303", "600002505", "fhd"),
    ("ahws", "安徽卫视", "2024171403", "600002532", "fhd"),
    ("hebws", "河北卫视", "2024171503", "600002493", "fhd"),
    ("sdws", "山东卫视", "2029787903", "600002513", "fhd"),
    ("tjws", "天津卫视", "2019927003", "600152137", "fhd"),
    ("jlws", "吉林卫视", "2025561503", "600190405", "fhd"),
    ("saxws", "陕西卫视", "2029795103", "600190400", "fhd"),
    ("nxws", "宁夏卫视", "2025608503", "600190737", "fhd"),
    ("nmgws", "内蒙古卫视", "2025561203", "600190401", "fhd"),
    ("ynws", "云南卫视", "2025561303", "600190402", "fhd"),
    ("shanxiws", "山西卫视", "2025560803", "600190407", "fhd"),
    ("gsws", "甘肃卫视", "2025561703", "600190408", "fhd"),
    ("qhws", "青海卫视", "2025559103", "600190406", "fhd"),
    ("xizangws", "西藏卫视", "2025558003", "600190403", "fhd"),
    ("xjws", "新疆卫视", "2019927403", "600152138", "fhd"),
    ("cetv1", "CETV-1", "2022823801", "600171827", "fhd"),
    ("cctvgx", "国学频道", "2029360403", "600213139", "fhd"),
)

CHANNEL_BY_SLUG = {row[0]: row for row in CHANNELS}

# iretcode 的含义，用于给出能看懂的日志
_ERR_HINT = {
    12: "应版权方要求暂停提供（平台授权问题，无法绕过）",
    14: "请求格式被拒绝（协议可能已变更）",
    25: "需要登录（该频道匿名接口不开放）",
    400: "参数错误（频道号可能失效）",
}


_pace_lock = threading.Lock()
_pace_last = 0.0

# 出口级熔断。上游按 IP 限流，一旦被限就是「所有频道一起 403」——这时候还会
# 把剩下几十个台挨个敲一遍，恰好把限流拖得更长，下次恢复得更慢。实测被限后
# 硬跑一轮要 4 分钟、结果 0 条；熔断后几秒就停手。
_deny_lock = threading.Lock()
_deny_streak = 0
_deny_until = 0.0
_DENY_STREAK = 5
_DENY_SECONDS = 60.0


def denied() -> bool:
    """出口是否正处于限流冷却中。"""
    return time.time() < _deny_until


def note_denied() -> None:
    """记一次「被上游拒绝」。连续几次就认为整条出口被限，停一分钟。"""
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


def reset_deny() -> None:
    """测试用：清掉熔断状态。"""
    global _deny_streak, _deny_until
    with _deny_lock:
        _deny_streak = 0
        _deny_until = 0.0


def pace(min_gap: float = 0.3) -> None:
    """全局节拍器：所有央视频请求串行发出。

    实测同一条链路上并发请求会被判为异常流量，返回的 403 和死链长得一样。
    间隔 0.3 秒时连续 10 次全部通过，再密就开始零星报错。
    """
    global _pace_last
    with _pace_lock:
        now = time.monotonic()
        wait = _pace_last + min_gap - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _pace_last = now


def _usable(resp) -> bool:
    """状态码 2xx 且有内容才算拿到东西。"""
    return 200 <= resp.status < 300 and bool(resp.body)


class YspError(RuntimeError):
    """取流失败。"""


class YspUnavailable(YspError):
    """频道被官方限制，重试也没用。"""


# --------------------------------------------------------------------------- #
# TEA 加密
# --------------------------------------------------------------------------- #
def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def _encrypt_block(block: bytes, key: bytes) -> bytes:
    """16 轮 TEA，大端序。"""
    y, z = struct.unpack(">2I", block)
    k = struct.unpack(">4I", key)
    total = 0
    for _ in range(16):
        total = _u32(total + 2654435769)
        y = _u32(y + _u32(_u32(_u32(z << 4) + k[0]) ^ _u32(z + total) ^ _u32((z >> 5) + k[1])))
        z = _u32(z + _u32(_u32(_u32(y << 4) + k[2]) ^ _u32(y + total) ^ _u32((y >> 5) + k[3])))
    return struct.pack(">2I", y, z)


def _checksum(buf: bytes) -> int:
    """滚动校验：v = 131*v + b，取 31 位。"""
    v = 0
    for b in buf:
        v = 131 * v + b & 0x7FFFFFFF
    return v


def _encrypt_packet(data: bytes, key: bytes) -> bytes:
    """TEA 加密，CBC 式链接 + 一字节填充长度，前置随机字节。"""
    pad = (8 - (len(data) + 10) % 8) % 8
    plain = bytes([os.urandom(1)[0] & 0xF8 | pad]) + os.urandom(pad) + os.urandom(2) + data + bytes(7)
    out, prev_plain, prev_cipher = b"", bytes(8), bytes(8)
    for off in range(0, len(plain), 8):
        mixed = bytes((a ^ b for a, b in zip(plain[off:off + 8], prev_cipher)))
        cipher = bytes((a ^ b for a, b in zip(_encrypt_block(mixed, key), prev_plain)))
        out += cipher
        prev_plain, prev_cipher = mixed, cipher
    return out


def _lp(text: str) -> bytes:
    """16 位长度前缀 + 内容。"""
    raw = text.encode() if isinstance(text, str) else text
    return struct.pack(">H", len(raw)) + raw


def _xor(data: bytes, key: bytes) -> bytes:
    n = len(key)
    return bytes((a ^ key[i % n] for i, a in enumerate(data)))


def _make_guard(ts: int, guid: str) -> str:
    """校验串：再套一层 TEA + 独立掩码。"""

    def tail(value: object) -> str:
        text = str(value)
        return text[-5:] if len(text) >= 5 else ""

    body = struct.pack(">I", ts) + _lp(tail(guid)) + _lp(tail("null")) + _lp(tail("null")) + _lp("-1")
    plain = _lp(body)
    enc = _encrypt_packet(plain, _GUARD_KEY) + struct.pack(">I", _checksum(plain))
    return _xor(enc, _GUARD_XOR).hex().upper()


def make_ckey(channel_id: str) -> dict:
    """构造 cKey。字段顺序不能动，长度前缀是协议的一部分。"""
    ts = int(time.time())
    guid = os.urandom(16).hex()
    guard = _make_guard(ts, guid)
    uid = os.urandom(4).hex().upper()

    body = (
        bytes.fromhex("0000004200000004000004d2")
        + struct.pack(">I", _PLATFORM)
        + struct.pack(">I", 0)
        + struct.pack(">I", ts)
        + _lp("dcgh")
        + _lp("_zj1A5Gh6QYcxWjIUGos2w==")
        + _lp(_APP_VERSION)
        + _lp(str(channel_id))
        + _lp(guid)
        + struct.pack(">I", 1)
        + struct.pack(">I", 1)
        + _lp(uid)
        + _lp("nil")
        + _lp("57eab0c4-2c58-44c6-8ae9-dd2757525dc5")
        + _lp("nil")
        + _lp("v0.1.000")
        + _lp("com.cctv.yangshipin.app.iphone")
        + _lp(str(_PLATFORM))
        + _lp("ex_json_bus")
        + _lp("ex_json_vs")
        + _lp(guard)
    )

    packet = bytearray(struct.pack(">H", len(body)) + body)
    # 第 18..22 字节是包内校验位，必须在整包算好后再回填
    packet[18:22] = struct.pack(">I", _checksum(bytes(packet)))
    raw = bytes(packet)

    enc = _encrypt_packet(raw, _TEA_KEY) + struct.pack(">I", _checksum(raw))
    b64 = base64.b64encode(_xor(enc, _XOR_KEY)).decode().replace("+", "_").replace("/", "-").rstrip("=")
    return {
        "cKey": "--01" + b64,
        "guid": guid,
        "ts": ts,
        "flowId": "%s_%d" % (uuid.uuid4().hex.upper(), _PLATFORM),
    }


# --------------------------------------------------------------------------- #
# 取流
# --------------------------------------------------------------------------- #
def play_info(cnlid: str, livepid: str, defn: str = "fhd", client: HttpClient | None = None) -> dict:
    """换取频道播放信息，原样返回接口的 JSON。"""
    if denied():
        raise YspError("上游限流中，暂停请求 60 秒")
    token = make_ckey(cnlid)
    query = urlencode({
        "atime": "120", "livepid": livepid, "cnlid": cnlid,
        "appVer": _APP_VERSION, "app_version": "300090", "caplv": "1",
        "cmd": "2", "defn": defn, "device": "iPhone", "encryptVer": "4.2",
        "getpreviewinfo": "0", "hevclv": "0", "lang": "zh-Hans_CN", "livequeue": "0",
        "logintype": "1", "nettype": "1", "newnettype": "1",
        "newplatform": str(_PLATFORM), "platform": str(_PLATFORM),
        "sdtfrom": "v3021", "spacode": "23", "spaudio": "1", "spdemuxer": "6",
        "spdrm": "2", "spdynamicrange": "1", "spflv": "1", "spflvaudio": "1",
        "sphdrfps": "60", "sphttps": "1", "spvcode": _VCODEC_CAP, "spvideo": "4",
        "stream": "1", "system": "1", "sysver": "ios18.2.1", "uhd_flag": "0",
        "cKey": token["cKey"], "guid": token["guid"], "fntick": str(token["ts"]),
        "flowid": token["flowId"], "playbacktime": "0",
    })

    client = client or HttpClient(user_agent=UA, timeout=10, retries=2)
    pace()
    resp = client.get(_INFO_URL + "?" + query, headers={"Accept": "application/json"})
    if resp.status in (403, 429):
        note_denied()
    elif _usable(resp):
        note_ok()
    if not _usable(resp):
        raise YspError(f"接口无响应（{resp.error or resp.status}）")

    try:
        data = json.loads(resp.text)
    except ValueError as exc:
        raise YspError("接口返回不是 JSON") from exc

    code = int(data.get("iretcode", -1))
    if code != 0:
        hint = _ERR_HINT.get(code, str(data.get("errinfo") or ""))
        cls = YspUnavailable if code == 12 else YspError
        raise cls(f"iretcode={code} {hint}")
    return data


def play_urls(info: dict) -> list[str]:
    """从播放信息里挑出所有候选地址，主线路在前。"""
    urls: list[str] = []
    primary = info.get("playurl") or ""
    if primary:
        urls.append(primary)
    for item in info.get("backurl_list") or []:
        url = item if isinstance(item, str) else (item.get("url") or "")
        if url:
            urls.append(url)
    # 去重且保序
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def seg_url(playlist_url: str, seg_line: str) -> str:
    """把分片行拼到 token 目录下。

    playurl 形如 ``…/<token>/<cnlid>.m3u8``，分片必须落在 ``…/<token>/`` 里。
    直接 urljoin 会丢掉 token，得到根路径 → 403。
    """
    base = urlsplit(playlist_url)
    seg = urlsplit(seg_line)
    path = base.path.rstrip("/")
    if path.endswith(".m3u8"):
        path = path.rsplit("/", 1)[0]
    return urlunsplit((base.scheme, base.netloc, path + "/" + seg.path.lstrip("/"),
                       seg.query or base.query, ""))


def _segment_seconds(text: str) -> float:
    """从媒体清单里读分片时长：优先最后一个 EXTINF，否则用 TARGETDURATION。"""
    last_extinf = 0.0
    target = 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                last_extinf = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = float(line.split(":", 1)[1])
            except ValueError:
                pass
    return last_extinf or target


def parse_segments(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def resolve(
    cnlid: str,
    livepid: str,
    defn: str = "fhd",
    client: HttpClient | None = None,
    verify_segment: bool = False,
    attempts: int = 3,
    pause: float = 1.2,
    meta: dict | None = None,
) -> tuple[str, dict, str]:
    """换一个当前确实能取到列表的地址，返回 (地址, 播放信息, 首个分片地址)。

    边缘节点是随机分配的，个别节点会直接 403 —— 换个 token 重来往往就好了。
    所以这里把「换地址 + 立刻验证」做成循环，把概率问题收敛在取源阶段，
    下游拿到的就是能用的地址。

    ``verify_segment`` 打开时连分片也取一小段（更严格，但请求量翻倍，
    批量跑时容易触发限流，默认只验证播放列表）。

    传 ``meta`` 字典可以把顺手读到的清单信息带出来（分片时长等），
    用来从分片大小推算真实码率，不必为它多发一次请求。
    """
    client = client or HttpClient(user_agent=UA, timeout=10, retries=2)
    last = "取流失败"
    for round_no in range(max(1, attempts)):
        try:
            info = play_info(cnlid, livepid, defn, client)
        except YspUnavailable:
            raise
        except YspError as exc:
            last = str(exc)
            if "限流" in last:
                # 出口已经进冷却，重试只是陪跑，立即交给上层跳过
                break
            time.sleep(pause)
            continue

        urls = play_urls(info)
        if not urls:
            last = "接口没有给出地址"
            time.sleep(pause)
            continue

        forbidden = 0
        for url in urls:
            pace()
            resp = client.get(url, limit=131072, headers=HLS_HEADERS)
            if resp.status in (403, 429):
                forbidden += 1
                note_denied()
            elif resp.status == 200:
                note_ok()
            if not _usable(resp) or not resp.body.lstrip().startswith(b"#EXTM3U"):
                last = f"列表 {resp.status or resp.error or '非播放列表'}"
                continue
            segs = parse_segments(resp.text)
            if not segs:
                last = "列表为空"
                continue
            first = seg_url(url, segs[0])
            if meta is not None:
                meta["segment_seconds"] = _segment_seconds(resp.text)
                meta["segments"] = len(segs)
            if not verify_segment:
                return url, info, first
            # 顺手读一片：确认真能出流，并拿到测速和 SPS 里的真实分辨率。
            # 读不到**不算失败**——清单已经证明这条地址是活的，分片被拒多半只是
            # 出口限流（403），拿它否掉一条能播的流是过去「好多台没拉取到」的主因。
            pace()
            probe = client.get(first, limit=SAMPLE_BYTES, headers=HLS_HEADERS)
            if meta is not None:
                meta["segment_status"] = probe.status
                if _usable(probe):
                    meta["segment_body"] = probe.body
                    meta["speed_mbps"] = probe.speed_mbps
                    meta["ttfb_ms"] = probe.ttfb_ms
            return url, info, first

        if round_no + 1 < attempts:
            # 主备 CDN 全 403 = 本机出口正被官方限流，等久一点再换票，
            # 马上重试只会把限流拖得更长。
            all_forbidden = bool(urls) and forbidden == len(urls)
            time.sleep(pause if not all_forbidden else pause + 3.0 * (round_no + 1))

    raise YspError(last)
