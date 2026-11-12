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

# 公共节目单源：epg.pw 的国内频道表，用 display-name 反查 id
DEFAULT_EPG_SOURCE = "https://epg.pw/xmltv/epg_CN.xml"

DEFAULT_CONFIG = """\
[app]
name = "hd-iptv"
data_dir = "output"

[update]
interval_hours = 24
run_on_start = true

[fetch]
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
timeout = 8
retries = 2
http_proxy = ""

[probe]
enabled = true
concurrency = 24
sample_bytes = 262144
# 单条流探测的总时限（秒）。慢滴流到点即弃，避免整轮更新卡死。
timeout = 15
# 整批探测的总预算（秒）。到点用已回来的结果收工。
budget = 180
min_speed_mbps = 0.8
min_height = 720
max_height = 2160
max_per_channel = 4
allow_unknown_resolution = false
probe_playlist_children = true

[relay]
playlist_ttl = 5.0
cache_mb = 96
mode = "direct"

[epg]
source = "https://epg.pw/xmltv/epg_CN.xml"
ttl_hours = 6

[output]
epg_url = ""
logo_base = ""
public_url = ""
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

    # ---- 常用项 ----
    @property
    def data_dir(self) -> Path:
        p = Path(self.get("app", "data_dir", "output"))
        return p if p.is_absolute() else self.root / p

    @property
    def user_agent(self) -> str:
        return os.environ.get("HDIPTV_USER_AGENT") or self.get("fetch", "user_agent", "")

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
        return int(os.environ.get("HDIPTV_SAMPLE_BYTES") or self.get("probe", "sample_bytes", 262144))

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
        return float(os.environ.get("HDIPTV_PROBE_BUDGET") or self.get("probe", "budget", 180))

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
    def public_url(self) -> str:
        """对外地址。面板里填过就用面板的，其次环境变量，最后配置文件。

        留空时由服务端按请求的 Host / X-Forwarded-Host 现算，反代场景不用手改。
        """
        panel = self.panel_get("public_url")
        if panel is not None:
            return str(panel).strip().rstrip("/")
        return (os.environ.get("PUBLIC_URL") or self.get("output", "public_url", "")).strip().rstrip("/")

    @property
    def region_hint(self) -> str:
        """手动指定省份（拼音，如 ``hubei``）。

        只在自动识别判错时才需要填 —— IP 归属地库偶尔会把跨省出口判成邻省。
        填了就以它为准，不再看 IP 库。
        """
        return (os.environ.get("HDIPTV_REGION") or self.get("output", "region", "")).strip().lower()

    @property
    def isp_hint(self) -> str:
        """手动指定运营商：``mobile`` / ``telecom`` / ``unicom`` / ``broadnet``。"""
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
    """
    if not path.exists():
        return False
    try:
        current = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    original = current
    if "max_height = 1080" in current:
        current = current.replace(
            "max_height = 1080",
            "max_height = 2160            # 画质上限：放开到 4K。官方源常常只有 4K 一路，\n"
            "                             # 卡在 1080 会把唯一可用的地址整条丢掉",
            1,
        )
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
