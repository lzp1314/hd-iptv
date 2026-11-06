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
    "playlist_ttl": (0.5, 60.0),
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
min_speed_mbps = 1.5
min_height = 720
max_height = 1080
max_per_channel = 4
allow_unknown_resolution = false
probe_playlist_children = true

[relay]
playlist_ttl = 5
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


def coerce_panel(raw: dict) -> dict:
    """把面板提交的内容收敛成合法值，非法项直接丢掉。"""
    out: dict = {}
    for key, value in (raw or {}).items():
        if key in ("public_url", "epg_url", "epg_source"):
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
    def min_speed_mbps(self) -> float:
        return float(os.environ.get("HDIPTV_MIN_SPEED") or self.get("probe", "min_speed_mbps", 1.5))

    @property
    def min_height(self) -> int:
        return int(os.environ.get("HDIPTV_MIN_HEIGHT") or self.get("probe", "min_height", 720))

    @property
    def max_height(self) -> int:
        """画质上限。默认 1080：4K 档匿名接口拿不到真 4K，留着只会多几个坏台。"""
        return int(os.environ.get("HDIPTV_MAX_HEIGHT") or self.get("probe", "max_height", 1080))

    @property
    def max_per_channel(self) -> int:
        return int(os.environ.get("HDIPTV_MAX_PER_CHANNEL") or self.get("probe", "max_per_channel", 4))

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
        """播放器取清单时，多久回上游要一次。越小越实时，越大越省请求。"""
        panel = self.panel_get("playlist_ttl")
        raw = panel if panel is not None else self.get("relay", "playlist_ttl", 5)
        try:
            return max(0.5, float(raw))
        except (TypeError, ValueError):
            return 2.0

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
    return settings


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
