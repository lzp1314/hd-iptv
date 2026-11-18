"""配置加载：config.toml + 环境变量覆盖。"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 包内自带的配置模板。容器里把 config/ 挂载成空目录时（最常见的一种翻车方式），
# 靠它把默认配置补齐，否则频道模板为空会直接产出 0 个频道。
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
DEFAULT_FILES = ("config.toml", "channels.txt", "sources.toml")
DEFAULT_CONFIG_DIRNAME = "config"

# 面板上可改的项存这里，而不是回写 config.toml：
# TOML 没有标准写入库，手写补丁容易把注释和格式搞乱。
PANEL_FILE = "panel.json"

# 允许面板修改的数值项及其取值范围，越界一律夹回区间内
PANEL_BOUNDS = {
    "interval_hours": (0.0, 168.0),
    "playlist_ttl": (1.0, 15.0),
    "cache_mb": (0.0, 4096.0),
}

_FALSE_WORDS = {"0", "false", "off", "no", "关", "关闭", ""}

# 面板上的「内置模块」到它包含的底层源名。
# 一层映射给三处共用：面板开关、采集跳过、订阅分组，都认展示名，关一个开关
# 就把它下面所有源一起关掉/摘掉，不会出现「列表合并了开关还是两个」的错位。
# 官方直连并入运营商官方源：两者都是国内官方 CDN 的直连地址，用户没必要分开管。
BUILTIN_MODULES: dict[str, tuple[str, ...]] = {
    "运营商官方源": ("运营商官方源", "官方直连", "移动IPTV-本省专用", "本省自动源"),
    "第三方聚合源": ("聚合源-多线路", "聚合源-公开转播", "聚合源-精简线路"),
    "央视频": ("综合频道", "央视官方"),
    "咪咕视频": ("咪咕视频",),
}


def display_module(source_name: str) -> str:
    """底层源名 → 面板展示的模块名。不在内置模块里的源原样返回。"""
    name = str(source_name or "").strip()
    for module, members in BUILTIN_MODULES.items():
        if name == module or name in members:
            return module
    return name


def module_members(module: str) -> tuple[str, ...]:
    """展示模块名 → 它包含的底层源名；非内置模块返回它自己。"""
    name = str(module or "").strip()
    return BUILTIN_MODULES.get(name, (name,))


def as_bool(value, default: bool = True) -> bool:
    """面板 checkbox / 环境变量 / TOML 三处来的开关统一收敛成布尔。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in _FALSE_WORDS

# 公共节目单源：epg.pw 的国内频道表，用 display-name 反查 id
DEFAULT_EPG_SOURCE = "https://epg.pw/xmltv/epg_CN.xml"

# 默认浏览器 UA。采集和「中转层替播放器取清单」都用它：采集阶段能用它取到清单，
# 说明上游认；换成别的（比如机顶盒 UA）反而会被聚合源拒。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_CONFIG = f"""\
[app]
name = "hd-iptv"
data_dir = "output"

[update]
interval_hours = 24
run_on_start = true
# 按本机宽带省份自动接入本省运营商单播清单（面板里也能开关，面板优先）
auto_region = true

[fetch]
user_agent = "{BROWSER_UA}"
timeout = 8
retries = 2
http_proxy = ""

[probe]
enabled = true
concurrency = 24
# 每条流采样多少字节来测速。256KB 在 4 Mbps 的 1080P 上只够读半秒，测得的是
# 「这一瞬间的爆发速度」，源头几十毫秒的抖动看不出来；512KB 才接近真实吞吐。
sample_bytes = 524288
# 单条流探测的总时限（秒）。慢滴流到点即弃，避免整轮更新卡死。
timeout = 15
# 整批探测的总预算（秒）。到点用已回来的结果收工。采样翻倍后要留出余量，
# 否则加严的代价会变成「排在后面的台根本没探到」。
budget = 210
min_speed_mbps = 0.8
# 运营商官方源单条探测时限（秒）。这类源是本省边缘，正常 0.1~0.2 秒就回；
# 挂着 10 秒的话，一条连不通的地址就白等 10 秒，上百条串行能拖成半小时。
official_timeout = 5
# 同一个台最多探几条官方地址。探通一条就算这组可用，其余直接当备用线路。
official_tries_per_channel = 2
# 连续几条超时就判定「这条网不通」，跳过剩余官方源。只认超时，不认 403/404。
official_timeout_streak = 5
# 实测速率要达到「该档画质所需带宽」的几成才算可用。0.5 意味着 1080P 只要
# 2 Mbps 就放行，而 1080P 真要 4 Mbps 才连续播——放进来就是卡一下停一下。
speed_ratio = 0.7
min_height = 720
max_height = 2160
max_per_channel = 4
allow_unknown_resolution = false
probe_playlist_children = true

[relay]
playlist_ttl = 5.0
cache_mb = 96
mode = "direct"
# 自动换源：播放中某条线路被上游拒、或者清单冻住时，是否自动切到这个台的
# 下一条线路。开着更抗封（一条挂了还有替补），关掉则固定第一条不变——排查
# 「到底哪条线路有问题」、或者线路之间来回跳的时候用。
auto_switch = true

[migu]
# 咪咕视频：央视/卫视等公开频道，播放时按 pID 现换票。
# 账号留空 = 游客（最高标清）；填了按账号权益给高清及以上。
# 账号与令牌只在本机参与换票，不写进播放地址、日志或订阅列表。
enabled = true
user_id = ""
token = ""
# 画质档位：2 标清 / 3 高清 / 4 蓝光 / 7 原画 / 9 4K
rate = 2
# H.265 默认关：面板用浏览器 hls.js，Chrome/Edge 的 MSE 解不了 H.265，
# 开了会一直转圈。要 H.265 的用户自己打开。
h265 = false
# 客户端就近取流默认关：它会把咪咕调度地址 302 给播放器，但咪咕最终节点
# 清单不带 CORS 头、且是 http，浏览器面板会被 CORS/混合内容拦死、VLC 也取不到。
# 关掉后改走本机取清单（同央视频），清单同源下发、分片播放器直连，开箱即播。
# 只在用原生播放器、且确认能取到清单时，才自己打开这个开关。
client_dispatch = false
refresh_minutes = 360

[epg]
source = "https://epg.pw/xmltv/epg_CN.xml"
ttl_hours = 6

[output]
epg_url = ""
logo_base = ""
public_url = ""
# 一个台有多条线路时，列表里只写一个固定地址，线路切换交给本机中转。
# 关掉就退回「每台一条直连地址」——那样一条线路被上游拒了就只能报错。
failover = true
"""


def read_panel(path: Path) -> dict:
    """读面板覆盖项，坏文件当作空。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clean_names(value) -> list[str]:
    """把面板提交的频道名列表收敛成干净、去重的字符串列表。

    频道名就是 channels.txt 里的那串字，不需要额外 id；限制条数和长度只是
    防止有人往 panel.json 里塞进几兆垃圾。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        name = str(item or "").strip()[:64]
        if name and name not in out:
            out.append(name)
        if len(out) >= 500:
            break
    return out


_SECRET_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_.~"
)


def clean_secret(value) -> str:
    """校验访问密码：留空 = 关掉密码，否则只能是 URL 安全字符。

    非法字符直接报错而不是悄悄删掉——用户会以为自己设的是原来那个密码，
    下次就进不去了。URL 里带中文、空格、斜杠在各个播放器上的编码方式不一致，
    与其猜，不如一开始就不要。
    """
    text = str(value or "").strip().strip("/")
    if not text:
        return ""
    bad = sorted({c for c in text if c not in _SECRET_CHARS})
    if bad:
        shown = " ".join("空格" if c.isspace() else c for c in bad[:5])
        raise ValueError(f"密码只能用字母、数字和 - _ . ~，这几个字符不行：{shown}")
    if len(text) > 64:
        raise ValueError("密码最多 64 个字符")
    return text


def coerce_panel(raw: dict) -> dict:
    """把面板提交的内容收敛成合法值，非法项直接丢掉。"""
    out: dict = {}
    for key, value in (raw or {}).items():
        if key == "hidden":
            out[key] = clean_names(value)
        elif key == "disabled_modules":
            # 面板关掉的源模块。存名字而不是下标：sources.toml 增删条目后
            # 下标会错位，名字才能稳稳对上同一个模块。
            raw_list = value if isinstance(value, list) else str(value or "").split(",")
            # 归一到展示模块名：旧配置里存的「官方直连」「综合频道」等底层名，
            # 升级后要和合并后的内置模块对上，不能因为改了名字就失效。
            names = {
                display_module(str(x).strip()[:128]) for x in raw_list
                if isinstance(x, str) and x.strip()
            }
            out[key] = sorted(names)
        elif key == "secret":
            # 只去掉两头的空白和斜杠，字符是否合法由 apply_settings 判定（那边能回 400）
            out[key] = str(value or "").strip().strip("/")
        elif key in ("public_url", "epg_url", "epg_source"):
            text = str(value or "").strip()
            out[key] = text.rstrip("/") if key == "public_url" else text
        elif key == "play_mode":
            text = str(value or "").strip().lower()
            if text in ("direct", "proxy"):
                out[key] = text
        elif key == "auto_region":
            out[key] = as_bool(value)
        elif key == "auto_switch":
            out[key] = as_bool(value)
        elif key == "migu_enabled":
            out[key] = as_bool(value)
        elif key == "migu_h265":
            out[key] = as_bool(value)
        elif key == "migu_client_dispatch":
            out[key] = as_bool(value)
        elif key == "migu_user_id":
            # 留空 = 清空账号（连同令牌一起，见 apply_settings）
            out[key] = str(value or "").strip()[:256]
        elif key == "migu_token":
            # 留空 = 不修改（令牌不回传给面板，前端框始终是空的）。
            # 要清空账号就把 migu_user_id 留空，那边会连同令牌一起清掉。
            text = str(value or "").strip()[:256]
            if text:
                out[key] = text
        elif key == "migu_rate":
            try:
                rate = int(value)
            except (TypeError, ValueError):
                continue
            if rate in (2, 3, 4, 7, 9):
                out[key] = rate
        elif key == "migu_refresh":
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num == num:
                out[key] = min(1440.0, max(1.0, num))
        elif key in ("region", "isp"):
            # 空串是有效值：表示「回到自动识别」。非空则归一化成拼音代码，
            # 认不出来就丢掉这一项（别把半截输入写进 panel.json 让采集去猜）。
            text = str(value or "").strip().lower()
            if not text:
                out[key] = ""
            else:
                from .region import ISP_NAMES, PROVINCES, isp_of, province_of

                if key == "region":
                    code = province_of(text)
                    if code in PROVINCES:
                        out[key] = code
                else:
                    code = isp_of(text) or text
                    if code in ISP_NAMES:
                        out[key] = code
        elif key in PANEL_BOUNDS:
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num != num:  # NaN
                continue
            low, high = PANEL_BOUNDS[key]
            num = max(low, min(high, num))
            out[key] = int(round(num)) if key == "cache_mb" else round(num, 2)
    return out


def write_panel(path: Path, values: dict) -> dict:
    """合并写入并返回完整内容。"""
    path = Path(path)
    merged = read_panel(path)
    merged.update(coerce_panel(values))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return merged


@dataclass(slots=True)
class Settings:
    root: Path
    data: dict = field(default_factory=dict)
    channels_file: Path = field(default_factory=lambda: Path("config/channels.txt"))
    sources_file: Path = field(default_factory=lambda: Path("config/sources.toml"))
    # 面板上改过的项（panel.json），优先级高于 config.toml 与环境变量
    panel: dict = field(default_factory=dict)
    panel_path: Path = field(default_factory=lambda: Path("config/panel.json"))
    # 本次加载时把旧的默认频道模板换成了新版（只发生一次，用来打日志）
    channels_upgraded: bool = False
    config_upgraded: bool = False

    # ---- 便捷读取 ----
    def section(self, name: str) -> dict:
        v = self.data.get(name, {})
        return v if isinstance(v, dict) else {}

    def get(self, section: str, key: str, default=None):
        return self.section(section).get(key, default)

    def panel_get(self, key: str):
        return self.panel.get(key)

    @property
    def disabled_modules(self) -> list[str]:
        """面板上关掉的源模块名字。"""
        raw = self.panel_get("disabled_modules")
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def module_disabled(self, name: str) -> bool:
        """这个源（或它所属的内置模块）是不是被面板关掉了。

        传进来的可以是底层源名（「官方直连」）也可以是展示模块名（「运营商官方源」），
        统一归一到展示名再判断：关掉一个内置模块，它下面所有源一起算关。
        一处判断同时管采集和订阅输出，不会出现「不采集了但列表里还在」的错位。
        """
        text = str(name or "").strip()
        if not text:
            return False
        return display_module(text) in set(self.disabled_modules)

    # ---- 常用项 ----
    @property
    def data_dir(self) -> Path:
        p = Path(self.get("app", "data_dir", "output"))
        return p if p.is_absolute() else self.root / p

    @property
    def user_agent(self) -> str:
        """取清单用的 UA。留空时回落到默认浏览器 UA，而不是空串。

        中转层替播放器取清单时也用这一个：留空会一路传到 ``urllib``，最后
        落成 ``qqlive``——那是央视频的客户端标识，聚合源看到它就 403。
        """
        return os.environ.get("HDIPTV_USER_AGENT") or self.get("fetch", "user_agent", "") or BROWSER_UA

    @property
    def timeout(self) -> float:
        return float(os.environ.get("HDIPTV_TIMEOUT") or self.get("fetch", "timeout", 8))

    @property
    def retries(self) -> int:
        return int(os.environ.get("HDIPTV_RETRIES") or self.get("fetch", "retries", 2))

    @property
    def proxy(self) -> str:
        return os.environ.get("HDIPTV_PROXY") or self.get("fetch", "http_proxy", "")

    @property
    def concurrency(self) -> int:
        return int(os.environ.get("HDIPTV_CONCURRENCY") or self.get("probe", "concurrency", 24))

    @property
    def sample_bytes(self) -> int:
        """每条流采样多少字节测速。

        256KB 在 4 Mbps 的 1080P 上只值半秒，测出来的是「这一瞬间的爆发速度」，
        上游偶尔快一下就会被当成好源；512KB 才接近真实吞吐。调小可以省时间，
        但代价是更容易把抖动当能力。
        """
        return int(os.environ.get("HDIPTV_SAMPLE_BYTES") or self.get("probe", "sample_bytes", 524288))

    @property
    def probe_timeout(self) -> float:
        """单条流探测的绝对时限（秒）。

        socket 超时只管「多久没数据」，管不住「一直慢慢挤数据」。后者会让
        整轮更新无限期挂住，所以这里给每条流一个总时限。
        """
        return float(os.environ.get("HDIPTV_PROBE_TIMEOUT") or self.get("probe", "timeout", 15))

    @property
    def probe_budget(self) -> float:
        """整批探测的总预算（秒）。到点就用已回来的结果收工。"""
        return float(os.environ.get("HDIPTV_PROBE_BUDGET") or self.get("probe", "budget", 210))

    @property
    def official_timeout(self) -> float:
        """运营商官方源单条探测的时限（秒）。

        这类源是本省边缘，实测正常 0.1~0.2 秒就回 200。挂着 10 秒的话，一条连不
        通的地址就要白等 10 秒；143 条串行排下来是半小时，面板上看着就像卡死。
        """
        raw = os.environ.get("HDIPTV_OFFICIAL_TIMEOUT") or self.get("probe", "official_timeout", 5)
        try:
            return min(30.0, max(1.0, float(raw)))
        except (TypeError, ValueError):
            return 5.0

    @property
    def official_tries_per_channel(self) -> int:
        """同一个台最多探几条官方地址。探通一条就算这组可用，其余继承结论当备用。"""
        raw = os.environ.get("HDIPTV_OFFICIAL_TRIES") or self.get(
            "probe", "official_tries_per_channel", 2
        )
        try:
            return max(1, min(4, int(raw)))
        except (TypeError, ValueError):
            return 2

    @property
    def official_timeout_streak(self) -> int:
        """连续几条超时就判定「这条网不通」，跳过剩余官方源。

        只认超时，不认 403/404：后者是「这个台没了」，不是「整条网不通」，
        拿去熔断会把能用的台一起丢掉。
        """
        raw = os.environ.get("HDIPTV_OFFICIAL_TIMEOUT_STREAK") or self.get(
            "probe", "official_timeout_streak", 5
        )
        try:
            return max(2, min(30, int(raw)))
        except (TypeError, ValueError):
            return 5

    @property
    def speed_ratio(self) -> float:
        """实测速率要打到「该档画质所需带宽」的几成才算可用。

        0.5 太松：1080P 只要 2 Mbps 就放行，而它真要 4 Mbps 才连续播，结果就是
        播几秒卡一下。0.7 是留了线路波动余量的值——再往上会把「刚好够」的源全
        砍光，那些源白天其实能看。夹在 0.3~1.0 之间，防止配错成 0 或 5。
        """
        raw = os.environ.get("HDIPTV_SPEED_RATIO") or self.get("probe", "speed_ratio", 0.7)
        try:
            return min(1.0, max(0.3, float(raw)))
        except (TypeError, ValueError):
            return 0.7

    @property
    def min_speed_mbps(self) -> float:
        return float(os.environ.get("HDIPTV_MIN_SPEED") or self.get("probe", "min_speed_mbps", 0.8))

    @property
    def min_height(self) -> int:
        return int(os.environ.get("HDIPTV_MIN_HEIGHT") or self.get("probe", "min_height", 720))

    @property
    def max_height(self) -> int:
        """画质上限。默认 2160：芒果、浙江广电这些官方源常常只给 4K 一路，
        卡在 1080 会把唯一可用的地址整条丢掉，然后退到 1 Mbps 的中转空壳上。
        环境变量 ``HDIPTV_MAX_HEIGHT`` 优先级最高，compose 里写了就以它为准。
        """
        return int(os.environ.get("HDIPTV_MAX_HEIGHT") or self.get("probe", "max_height", 2160))

    @property
    def max_per_channel(self) -> int:
        return int(os.environ.get("HDIPTV_MAX_PER_CHANNEL") or self.get("probe", "max_per_channel", 4))

    @property
    def lines_per_channel(self) -> int:
        """订阅列表里每个台写几行。

        默认 1：VLC 这类播放器不会按台名归并，写几条就列几行，看上去就是一排
        重复台。备用线路仍然保留在 result.json 里，想手动切线路的调大这个值。
        """
        raw = os.environ.get("HDIPTV_LINES_PER_CHANNEL") or self.get("output", "lines_per_channel", 1)
        try:
            return max(1, min(8, int(raw)))
        except (TypeError, ValueError):
            return 1

    @property
    def allow_unknown(self) -> bool:
        return bool(self.get("probe", "allow_unknown_resolution", False))

    @property
    def failover(self) -> bool:
        """一个台有多条线路时，是否启用「单地址多线路自动切换」。

        开：列表里写 ``/live/c:<台名>.m3u8``，中转层内部按顺序换线路，
        播放器只认一个地址，一条被上游拒了也不会「无法播放」。
        关：退回每台一条直连地址（旧行为），排查「到底哪条线路有问题」时用。
        """
        raw = os.environ.get("HDIPTV_FAILOVER")
        if raw is not None and str(raw).strip():
            return as_bool(raw)
        return as_bool(self.get("output", "failover", True))

    @property
    def auto_switch(self) -> bool:
        """播放中是否允许自动换源（切到这个台的下一条线路）。

        开（默认）：一条线路被上游拒、或者清单不往前滚，就换下一条。一个台挂了
        替补，用户不用手动切。

        关：固定用排序第一的那条，取不到就如实报错，不再自己跳。线路之间来回
        跳（两条都有问题、或者判定过严）时用它先把画面稳下来；也用来定位「到底
        是哪条线路在坏」。

        关掉不影响「换票重取」：同一个地址重试还是照常，只是不再换到别的地址。

        优先级：面板 > 环境变量 > 配置文件。面板排第一是因为这个开关是给「正在
        卡着看的人」用的——他点了要立刻见效，不能被部署时写的环境变量压回去。
        """
        panel = self.panel_get("auto_switch")
        if panel is not None:
            return as_bool(panel)
        raw = os.environ.get("HDIPTV_AUTO_SWITCH")
        if raw is not None and str(raw).strip():
            return as_bool(raw)
        return as_bool(self.get("relay", "auto_switch", True))

    @property
    def public_url(self) -> str:
        """对外地址。面板里填过就用面板的，其次环境变量，最后配置文件。

        留空时由服务端按请求的 Host / X-Forwarded-Host 现算，反代场景不用手改。
        """
        panel = self.panel_get("public_url")
        if panel is not None:
            return str(panel).strip().rstrip("/")
        return (os.environ.get("PUBLIC_URL") or self.get("output", "public_url", "")).strip().rstrip("/")

    @property
    def auto_region(self) -> bool:
        """是否按本机宽带省份自动接入本省运营商单播清单。

        面板开关优先，其次 ``HDIPTV_AUTO_REGION``，最后 config.toml；默认开。
        面板里存过一次值（哪怕是关）就以它为准，免得容器环境变量把用户
        在面板上的选择又顶回去。
        """
        panel = self.panel_get("auto_region")
        if panel is not None:
            return as_bool(panel)
        raw = os.environ.get("HDIPTV_AUTO_REGION")
        if raw is not None and str(raw).strip():
            return as_bool(raw)
        return as_bool(self.get("update", "auto_region", True))

    @property
    def region_hint(self) -> str:
        """手动指定省份（拼音，如 ``hubei``）。优先级：面板 > 环境变量 > config.toml。

        面板里存空串表示「回到自动识别」，所以必须用 ``is not None`` 判断，
        不能用 ``or``——否则用户主动选回「自动」会被环境变量顶回去。
        """
        panel = self.panel_get("region")
        if panel is not None:
            return str(panel).strip().lower()
        return (os.environ.get("HDIPTV_REGION") or self.get("output", "region", "")).strip().lower()

    @property
    def isp_hint(self) -> str:
        """手动指定运营商：``mobile`` / ``telecom`` / ``unicom`` / ``broadnet``。

        优先级同 ``region_hint``：面板 > 环境变量 > config.toml，空串=自动识别。
        """
        panel = self.panel_get("isp")
        if panel is not None:
            return str(panel).strip().lower()
        return (os.environ.get("HDIPTV_ISP") or self.get("output", "isp", "")).strip().lower()

    @property
    def secret(self) -> str:
        """访问密码：URL 里的一段必填路径。

        设了之后所有地址都得带它（``/密码/playlist.m3u``、``/密码/live/x.m3u8``），
        不带或带错一律回「身份认证失败」，连面板首页都进不去。留空 = 不设密码，
        行为和以前完全一样。

        优先级：面板 > 环境变量 ``HDIPTV_SECRET`` > config.toml ``[output] secret``。
        面板改的落在 ``config/panel.json``（已在 .gitignore 里），刷新镜像不会丢，
        也不会跟着仓库走；忘了密码就删掉这一行，或把 panel.json 里的 secret 删掉。
        """
        panel = self.panel_get("secret")
        if panel is not None:
            return str(panel).strip().strip("/")
        raw = (os.environ.get("HDIPTV_SECRET") or self.get("output", "secret", "") or "").strip()
        return raw.strip("/")

    @property
    def epg_url(self) -> str:
        panel = self.panel_get("epg_url")
        if panel is not None:
            return str(panel).strip()
        return str(self.get("output", "epg_url", "") or "").strip()

    @property
    def logo_base(self) -> str:
        return self.get("output", "logo_base", "")

    @property
    def epg_source(self) -> str:
        panel = self.panel_get("epg_source")
        if panel is not None:
            return str(panel).strip()
        return str(self.get("epg", "source", DEFAULT_EPG_SOURCE) or "").strip()

    @property
    def epg_ttl_hours(self) -> float:
        return float(os.environ.get("HDIPTV_EPG_TTL_HOURS") or self.get("epg", "ttl_hours", 6))

    @property
    def playlist_ttl(self) -> float:
        """清单预取间隔（秒）。

        有人在看某个台时，后台每隔这么久替播放器去上游要一次新清单，播放器来请求
        就直接读内存。调小=更实时但更费出口配额，调大=省配额但滞后半拍。

        名字沿用 ``playlist_ttl``，是为了让已经在用的 panel.json / config.toml
        不用改；它早就不表示「清单缓存多久」了。
        """
        panel = self.panel_get("playlist_ttl")
        raw = panel if panel is not None else self.get("relay", "playlist_ttl", 5.0)
        try:
            # 下限 1 秒：比上游滚片（5 秒一片）还勤的请求有一半是重复拿到同一份
            # 清单，纯烧出口配额，反而是把自己打进限流的动作。
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            return 5.0

    @property
    def relay_cache_mb(self) -> float:
        """分片中转缓存上限（MB）。多客户端共用一个缓存，越小越容易被上游限流。"""
        panel = self.panel_get("cache_mb")
        raw = panel if panel is not None else os.environ.get("HDIPTV_RELAY_CACHE_MB") or self.get("relay", "cache_mb", 96)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 96.0

    @property
    def hidden(self) -> list[str]:
        """面板里隐藏掉的频道名。

        隐藏只影响对外输出（订阅列表 / txt / JSON），原始采集结果照旧保留在
        result.json 里——这样点一下「恢复」就回来了，不用重新跑一轮更新。
        """
        panel = self.panel.get("hidden")
        if panel is not None:
            return clean_names(panel)
        return clean_names(self.get("output", "hidden", []))

    @property
    def play_mode(self) -> str:
        """播放方式：``direct``（清单本机下发、分片直连 CDN）或 ``proxy``（全转发）。

        实测只有 direct 能稳定播：全转发时本机去拉分片会被平台当成异常流量回 403，
        而播放器自己直连带着 TLS 会话复用，反而没事。留在面板上是因为个别
        播放器/网络环境可能需要退回全转发。
        """
        panel = self.panel_get("play_mode")
        raw = panel if panel is not None else os.environ.get("HDIPTV_PLAY_MODE") or self.get("relay", "mode", "direct")
        text = str(raw or "").strip().lower()
        return text if text in ("direct", "proxy") else "direct"

    # ---- 咪咕视频 ----
    @property
    def has_migu_section(self) -> bool:
        """配置文件里有没有 [migu] 段。新模板自带，旧配置由升级补上。

        自建的最小配置（测试、精简部署）没有这一段，就不会凭空多一路网络采集。
        """
        return "migu" in self.data

    @property
    def migu_enabled(self) -> bool:
        """是否启用咪咕线路。面板开关优先，其次环境变量，最后配置文件。"""
        panel = self.panel_get("migu_enabled")
        if panel is not None:
            return as_bool(panel)
        raw = os.environ.get("HDIPTV_MIGU_ENABLED")
        if raw is not None and str(raw).strip():
            return as_bool(raw)
        return as_bool(self.get("migu", "enabled", True))

    def _migu_value(self, panel_key: str, env_key: str, section_key: str, default):
        panel = self.panel_get(panel_key)
        if panel is not None:
            return panel
        raw = os.environ.get(env_key)
        if raw is not None and str(raw).strip():
            return raw
        return self.get("migu", section_key, default)

    @property
    def migu_user_id(self) -> str:
        """咪咕账号 ID。只在本机参与换票，绝不进播放地址或日志。"""
        return str(self._migu_value("migu_user_id", "HDIPTV_MIGU_USER_ID", "user_id", "") or "").strip()

    @property
    def migu_token(self) -> str:
        return str(self._migu_value("migu_token", "HDIPTV_MIGU_TOKEN", "token", "") or "").strip()

    @property
    def migu_rate(self) -> int:
        """画质档位：2 标清 / 3 高清 / 4 蓝光 / 7 原画 / 9 4K。"""
        raw = self._migu_value("migu_rate", "HDIPTV_MIGU_RATE", "rate", 2)
        try:
            rate = int(raw)
        except (TypeError, ValueError):
            return 2
        return rate if rate in (2, 3, 4, 7, 9) else 2

    @property
    def migu_h265(self) -> bool:
        raw = self._migu_value("migu_h265", "HDIPTV_MIGU_H265", "h265", False)
        return as_bool(raw)

    @property
    def migu_client_dispatch(self) -> bool:
        """客户端就近取流：换票后把未跳转的调度地址 302 给播放器，由平台按
        播放器网络就近调度。默认关——咪咕最终节点清单不带 CORS 头且是 http，
        浏览器面板会被拦。关掉时咪咕改走本机取清单（同央视频），开箱即播。
        只有用原生播放器、且确认能取到清单时才打开。"""
        raw = self._migu_value("migu_client_dispatch", "HDIPTV_MIGU_CLIENT_DISPATCH",
                               "client_dispatch", False)
        return as_bool(raw)

    @property
    def migu_refresh_minutes(self) -> float:
        """频道表刷新间隔（分钟）。播放地址是点播时现换的，与它无关。"""
        raw = self._migu_value("migu_refresh", "HDIPTV_MIGU_REFRESH", "refresh_minutes", 360)
        try:
            return min(1440.0, max(1.0, float(raw)))
        except (TypeError, ValueError):
            return 360.0

    @property
    def interval_hours(self) -> float:
        panel = self.panel_get("interval_hours")
        if panel is not None:
            try:
                return max(0.0, float(panel))
            except (TypeError, ValueError):
                pass
        return float(os.environ.get("HDIPTV_INTERVAL_HOURS") or self.get("update", "interval_hours", 6))

    @property
    def run_on_start(self) -> bool:
        return bool(self.get("update", "run_on_start", True))


def ensure_defaults(root: Path | str) -> list[str]:
    """把缺失的默认配置补进 <root>/config/，已有文件一律不动。

    针对的场景：Docker 把宿主机上的空目录挂到 /app/config，镜像里原来那份
    配置被整个遮住，程序起来发现频道模板为空，一声不响地产出空结果。
    """
    cfg_dir = Path(root) / DEFAULT_CONFIG_DIRNAME
    created: list[str] = []
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return created

    for name in DEFAULT_FILES:
        target = cfg_dir / name
        if target.exists():
            continue
        source = DEFAULTS_DIR / name
        if not source.exists():
            continue
        try:
            shutil.copyfile(source, target)
        except OSError:
            continue
        created.append(name)
    return created


def load_settings(root: Path | str = ".", config_file: str | Path | None = None) -> Settings:
    root = Path(root).resolve()
    cfg_path = Path(config_file) if config_file else root / "config" / "config.toml"
    # 先就地把旧默认值改掉，再读进内存——挂载在宿主机的旧 config.toml 不会
    # 随镜像更新，不改的话「画质上限 1080」会一直把 4K 官方源整条挡掉。
    config_upgraded = upgrade_config(cfg_path)
    data: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fp:
            data = tomllib.load(fp)
    settings = Settings(root=root, data=data)
    settings.channels_file = root / "config" / "channels.txt"
    settings.sources_file = root / "config" / "sources.toml"
    settings.panel_path = cfg_path.parent / PANEL_FILE
    settings.panel = coerce_panel(read_panel(settings.panel_path))
    settings.channels_upgraded = upgrade_channels(settings.channels_file)
    settings.config_upgraded = config_upgraded
    return settings


def upgrade_config(path: Path) -> bool:
    """就地把旧默认 config.toml 里「会挡住功能」的默认值改掉。

    整份替换会把用户的自定义（并发数、UA、分组）一起冲掉，所以只动具体的键。

    目前两处：

    * ``max_height = 1080`` → 2160。很多官方源只提供 4K 一路（芒果、浙江广电
      都是），上限卡在 1080 会把唯一可用的那条地址整条丢掉，然后退到公开列表里
      1 Mbps 的中转空壳上——湖南卫视卡顿的根源就是这里。
    * ``playlist_ttl = 2.5`` → 5.0。2.5 秒比上游滚片（5 秒一片）还勤，一半请求
      拿到的是同一份清单，纯烧出口配额；出口被按 IP 记账打到限流之后，换新票也
      全是 403。这条只在文件里还是旧默认值时改，面板上的值优先。
    * ``sample_bytes = 262144`` → 524288。256KB 在 4 Mbps 的 1080P 上只值半秒，
      测出来的是爆发速度，上游偶尔快一下就被当成好源——这正是「看着有码率、
      播起来还是卡」的来源之一。
    * ``budget = 180`` → 210。采样翻倍后每条流多读一倍数据，预算不跟着放宽，
      加严的代价就变成「排在后面的台根本没轮到探测」。
    """
    if not path.exists():
        return False
    try:
        current = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    original = current
    if "sample_bytes = 262144" in current:
        current = current.replace("sample_bytes = 262144", "sample_bytes = 524288", 1)
        # 行内注释里写着「（256KB）」，不改就成了自相矛盾的注释
        current = current.replace("（256KB）", "（512KB）", 1)
    if "budget = 180" in current:
        current = current.replace("budget = 180", "budget = 210", 1)
    if "max_height = 1080" in current:
        current = current.replace(
            "max_height = 1080",
            "max_height = 2160            # 画质上限：放开到 4K。官方源常常只有 4K 一路，\n"
            "                             # 卡在 1080 会把唯一可用的地址整条丢掉",
            1,
        )
    # 旧配置没有 [migu] 段：补一段默认值。这样升级后咪咕源会被注入，
    # 而自建的最小配置（本来就刻意不写这段）保持原样。
    # 用 [output] 作为「这是完整项目配置」的标记：最小测试/精简配置没有它。
    if "[output]" in current and "[migu]" not in current:
        current = current.rstrip() + """\n
[migu]
# 咪咕视频：央视/卫视等公开频道，播放时现换票，由播放器就近取流。
# 账号留空 = 游客（最高标清）；填了按账号权益给高清及以上。
enabled = true
user_id = ""
token = ""
rate = 2
h265 = false
client_dispatch = false
refresh_minutes = 360
"""
    if "playlist_ttl = 2.5" in current:
        current = current.replace(
            "# 上游清单只有 3 个分片，服务端会把它们攒成 8 片的窗口再发出去。\n",
            "",
            1,
        )
        current = current.replace(
            "playlist_ttl = 2.5",
            "# 上游 5 秒滚一片，5 秒取一次刚好贴着直播边缘；取更勤有一半是重复拿到\n"
            "# 同一份清单，白烧出口配额，反而把自己打成限流（平台按出口 IP 记账）。\n"
            "# 上游清单只列 3 片（约 16 秒），服务端会把它们攒成 6 片的窗口再发出去——\n"
            "# 上限卡在 CDN 留存（实测 11 片）的一半，再攒就会把已被回收的分片写进清单。\n"
            "playlist_ttl = 5.0",
            1,
        )

    if current == original:
        return False
    try:
        path.write_text(current, encoding="utf-8")
    except OSError:
        return False
    return True


# 旧版默认频道模板的特征行。只在这句还在时替换——用户自己改过的模板一定
# 删掉了它，绝不会被这段逻辑覆盖。
_OLD_CHANNEL_MARKERS = (
    "地方台、港澳台、体育赛事、平台直播都不在",
    "频道越少，每轮探测越快",
)


def upgrade_channels(path: Path) -> bool:
    """把「没改过的旧默认频道模板」换成新的。

    Docker 用户把 config/ 挂到宿主机后，镜像里的新模板再也进不去，光改代码
    等于没改：面板上照样少十几个台。这里只在文件仍然是旧默认模板时替换，
    用户自己加过台的（那几句注释早被删了）一律不动。
    """
    source = DEFAULTS_DIR / "channels.txt"
    if not source.exists() or not path.exists():
        return False
    try:
        current = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if not any(marker in current for marker in _OLD_CHANNEL_MARKERS):
        return False
    try:
        shutil.copyfile(source, path)
    except OSError:
        return False
    return True


# 判断「这份 sources.toml 还是出厂的默认模板、没被人改过」。# 每次换主源，模板里的注释标题都会跟着变，这里把历史版本的特征行都列上，
# 命中任意一条就当成旧模板替换。新版自己的特征单独判，命中就绝不重复替换。
_OLD_SOURCES_MARKERS = (
    "静态开放源（主源）",            # 0.9.3：静态源当主源（移动专属，跨省/联通全 403）
    "静态开放源（默认关闭",          # 0.9.4：静态源关掉、央视频改回主源（仍会被按 IP 限流）
    "综合频道信息服务接口（主源）",   # 0.9.2 及更早：央视频全量 resolve，一次打五十多个请求
    "聚合源-多线路",                 # 0.9.5~0.9.8：只有公开聚合源，没有官方直连
    "官方直连",                      # 0.9.9~0.9.12：官方直连有了，但还没有本省运营商源
)
# 0.9.13：加上本省运营商官方源（移动/电信 IPTV 边缘），串行慢探、排在最前。
_NEW_SOURCES_MARKER = "运营商官方源"


def upgrade_sources(path: Path) -> bool:
    """把「没改过的旧默认采集源」换成当前版本的默认配置。

    ``ensure_defaults`` 只在文件不存在时才写，宿主机上早就存在的那份旧
    sources.toml 永远不会被换掉——用户删容器重建，起来的还是旧配置，
    表面上「重建了」实际什么都没变。这里补上这一刀。

    判断依据是模板里的注释标题：命中任一旧版特征、且不含新版特征，才替换。
    用户自己改动过源的（改过 channels、加了自己的源、删了这些注释），
    不会被误覆盖。
    """
    source = DEFAULTS_DIR / "sources.toml"
    if not source.exists() or not path.exists():
        return False
    try:
        current = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if _NEW_SOURCES_MARKER in current:
        return False
    if not any(marker in current for marker in _OLD_SOURCES_MARKERS):
        return False
    try:
        shutil.copyfile(source, path)
    except OSError:
        return False
    return True


# 0.9.18 新增的两条公开转播源。宿主机上挂载的旧 sources.toml 不会随镜像
# 更新，光改仓库里的模板对老用户没用，所以这里做一次「增量追加」：
# 只加缺的那两条，用户自己写的东西一个字不动。
_PUBLIC_SOURCES_MARKER = "# hd-iptv: public-sources-v1"
_PUBLIC_SOURCES_NAMES = ("聚合源-公开转播", "聚合源-精简线路")
PUBLIC_SOURCES_BLOCK = """

# hd-iptv: public-sources-v1 —— 0.9.18 起新增的两条公开转播源。
# 这行是升级标记：看到它就说明这两条源已经处理过。之后你删掉它们、
# 或者改了别的配置，程序都不会再塞回来。
# ---------------- 公开转播源（全国可用，补充候选线路） ----------------
# HerbertHe/iptv-sources 的成品清单，按内容类型分组、不按省份组织。
# 代价：单个第三方站点维护，随时可能失效或夹广告；探测阶段会按画质和
# 速率筛，坏线路自动淘汰，匹配不上频道模板的条目会被丢掉。
[[source]]
name = "聚合源-公开转播"
type = "subscription"
urls = ["https://m3u.ibert.me/cn.m3u"]
enabled = true

[[source]]
name = "聚合源-精简线路"
type = "subscription"
urls = ["https://m3u.ibert.me/cn_p.m3u"]
enabled = true
"""


def append_public_sources(path: Path) -> bool:
    """老用户的 sources.toml 缺这两条源时，追加一次。

    ``upgrade_sources()`` 只在「整个文件还是出厂模板」时才整份替换；
    已经含「运营商官方源」的文件动不了。这里补上增量升级。

    标记的作用是「只处理一次」：用户后来主动删掉这两条源，不该在每次
    更新时又被强行加回来。所以有标记就不再追加，哪怕源已经不在了。
    """
    path = Path(path)
    if not path.exists():
        return False
    try:
        current = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if _PUBLIC_SOURCES_MARKER in current:
        return False
    # 只对「还是出厂默认模板」的文件补源。自己写的一份精简 sources.toml
    # 不含这行特征，一个字都不动 —— 免得把用户没要的源硬塞进去。
    if _NEW_SOURCES_MARKER not in current:
        return False
    block = PUBLIC_SOURCES_BLOCK
    if any(name in current for name in _PUBLIC_SOURCES_NAMES):
        # 用户自己加过同名源：只补标记，不重复加
        block = f"\n{_PUBLIC_SOURCES_MARKER}（同名源已存在，未重复添加）\n"
    try:
        with path.open("a", encoding="utf-8") as fp:
            fp.write(block)
    except OSError:
        return False
    return block == PUBLIC_SOURCES_BLOCK
