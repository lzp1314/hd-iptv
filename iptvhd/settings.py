"""配置加载：config.toml + 环境变量覆盖。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
