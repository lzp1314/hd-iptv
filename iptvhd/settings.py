"""配置加载：config.toml + 环境变量覆盖。"""

from __future__ import annotations

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

DEFAULT_CONFIG = """\
[app]
name = "hd-iptv"
data_dir = "output"

[update]
interval_hours = 6
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
max_height = 2160
max_per_channel = 4
allow_unknown_resolution = false
probe_playlist_children = true

[output]
epg_url = ""
logo_base = ""
public_url = ""
"""


@dataclass(slots=True)
class Settings:
    root: Path
    data: dict = field(default_factory=dict)
    channels_file: Path = field(default_factory=lambda: Path("config/channels.txt"))
    sources_file: Path = field(default_factory=lambda: Path("config/sources.toml"))

    # ---- 便捷读取 ----
    def section(self, name: str) -> dict:
        v = self.data.get(name, {})
        return v if isinstance(v, dict) else {}

    def get(self, section: str, key: str, default=None):
        return self.section(section).get(key, default)

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
        """画质上限。默认 2160：源里真有 4K 就取，没有也不影响。"""
        return int(os.environ.get("HDIPTV_MAX_HEIGHT") or self.get("probe", "max_height", 2160))

    @property
    def max_per_channel(self) -> int:
        return int(os.environ.get("HDIPTV_MAX_PER_CHANNEL") or self.get("probe", "max_per_channel", 4))

    @property
    def allow_unknown(self) -> bool:
        return bool(self.get("probe", "allow_unknown_resolution", False))

    @property
    def public_url(self) -> str:
        return os.environ.get("PUBLIC_URL") or self.get("output", "public_url", "")

    @property
    def epg_url(self) -> str:
        return self.get("output", "epg_url", "")

    @property
    def logo_base(self) -> str:
        return self.get("output", "logo_base", "")

    @property
    def interval_hours(self) -> float:
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
    return settings
