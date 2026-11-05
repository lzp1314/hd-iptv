"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .pipeline import load_sources, run_update
from .settings import load_settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iptvhd",
        description="hd-iptv：高清 IPTV 直播源采集 / 清洗 / 测速 / 分发",
    )
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 <root>/config/config.toml）")
    parser.add_argument("-v", "--version", action="version", version=f"hd-iptv {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_update = sub.add_parser("update", help="执行一次完整更新")
    p_update.add_argument("--quiet", action="store_true", help="只输出最终结果")

    p_serve = sub.add_parser("serve", help="启动 Web 服务（含定时更新）")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)

    sub.add_parser("doctor", help="环境与配置自检")

    return parser


def cmd_update(root: Path, config: str | None, quiet: bool) -> int:
    settings = load_settings(root, config)
    log = _silent() if quiet else (lambda m: print(m, flush=True))
    report = run_update(settings, log=log)
    print(
        f"\n汇总：采集 {report.collected} 条 / 探测 {report.probed} 条 / 可用 {report.alive} 条 / "
        f"发布 {report.published} 条 / 命中频道 {report.channels_matched}/{report.channels_total} / "
        f"耗时 {report.elapsed:.1f}s"
    )
    return 0 if report.published else 2


def _silent():
    def log(message: str) -> None:
        if message.startswith(("[完成]", "[输出]", "!")):
            print(message, flush=True)

    return log


def cmd_serve(root: Path, config: str | None, host: str, port: int) -> int:
    from .service import serve

    settings = load_settings(root, config)
    try:
        serve(settings, host=host, port=port)
    except OSError as exc:
        print(f"服务启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(root: Path, config: str | None) -> int:
    settings = load_settings(root, config)
    ok = True
    print(f"hd-iptv {__version__}")
    print(f"Python {sys.version.split()[0]}  平台 {sys.platform}")

    cfg = root / "config" / "config.toml"
    print(f"配置文件：{cfg} {'✓' if cfg.exists() else '✗ 缺失，将使用内置默认值'}")

    channels = settings.channels_file
    if channels.exists():
        from .normalize import load_channels

        items = load_channels(channels)
        print(f"频道模板：{channels} ✓ {len(items)} 个频道")
        if not items:
            ok = False
    else:
        print(f"频道模板：{channels} ✗ 缺失")
        ok = False

    sources = load_sources(settings)
    enabled = [s for s in sources if s.get("enabled", True)]
    print(f"采集源：共 {len(sources)} 个，启用 {len(enabled)} 个")
    for item in enabled:
        print(f"  - [{item.get('type')}] {item.get('name') or ''} {item.get('url') or item.get('path') or ''}")
    if not enabled:
        print("  ✗ 没有启用的采集源，更新不会产出任何结果")
        ok = False

    out = settings.data_dir
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe_file = out / ".write_test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        print(f"输出目录：{out} ✓ 可写")
    except OSError as exc:
        print(f"输出目录：{out} ✗ {exc}")
        ok = False

    print(f"探测门槛：{settings.min_height}P 以上、{settings.min_speed_mbps}Mbps 以上，每频道最多 {settings.max_per_channel} 条")
    print(f"并发：{settings.concurrency}  超时：{settings.timeout}s")

    print("\n结论：" + ("配置可用，可以执行 update" if ok else "存在阻塞项，先按上面的 ✗ 修正"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.command == "update":
        return cmd_update(root, args.config, args.quiet)
    if args.command == "serve":
        return cmd_serve(root, args.config, args.host, args.port)
    if args.command == "doctor":
        return cmd_doctor(root, args.config)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
