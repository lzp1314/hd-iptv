"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .pipeline import load_sources, run_update
from .settings import ensure_defaults, load_settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iptvhd",
        description="hd-iptv：高清 IPTV 播放列表管理 / 清洗 / 测速 / 分发",
    )
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 <root>/config/config.toml）")
    parser.add_argument("-v", "--version", action="version", version=f"hd-iptv {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_update = sub.add_parser("update", help="执行一次完整更新")
    p_update.add_argument("--quiet", action="store_true", help="只输出最终结果")

    p_serve = sub.add_parser("serve", help="启动 Web 服务（含定时更新）")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=9797)

    sub.add_parser("doctor", help="环境与配置自检")

    p_probe = sub.add_parser("probe", help="探测地址的真实画质与速率（判断源能不能用）")
    p_probe.add_argument("urls", nargs="+", help="一个或多个 m3u8 地址")

    return parser


def cmd_probe(root: Path, config: str | None, urls: list[str]) -> int:
    """逐条探测地址：能不能出流、分辨率多少、速率多少。

    用来在把某个平台接入解析器之前先验证——比直接跑整轮更新快得多。
    """
    from .models import Stream
    from .net import build_client
    from .probe import probe_one

    settings = load_settings(root, config)
    client = build_client(
        user_agent=settings.user_agent,
        timeout=settings.timeout,
        retries=1,
        proxy=settings.proxy,
    )

    bad = 0
    print(f"{'结果':<6}{'画质':<10}{'速率':<12}{'延迟':<10}地址")
    print("-" * 88)
    for url in urls:
        stream = Stream(url=url)
        try:
            probe_one(stream, client, settings.sample_bytes)
        except Exception as exc:  # noqa: BLE001
            print(f"{'异常':<6}{'-':<10}{'-':<12}{'-':<10}{url}  ({exc})")
            bad += 1
            continue

        if stream.ok:
            size = f"{stream.width}x{stream.height}" if stream.height else "未知"
            print(
                f"{'可用':<6}{stream.grade + ' ' + size:<10}"
                f"{stream.speed_mbps:.1f}Mbps{'':<4}{stream.delay_ms}ms{'':<6}{url}"
            )
            if stream.height < settings.min_height:
                print(f"       ↑ 低于门槛 {settings.min_height}P，更新时会被筛掉")
            elif settings.max_height and stream.height > settings.max_height:
                print(f"       ↑ 高于上限 {settings.max_height}P，更新时会被筛掉")
        else:
            print(f"{'不可用':<6}{'-':<10}{'-':<12}{'-':<10}{url}  ({stream.error})")
            bad += 1

    return 0 if bad < len(urls) else 2


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

    print(
        f"探测门槛：{settings.min_height}~{settings.max_height}P、"
        f"{settings.min_speed_mbps}Mbps 以上，每频道最多 {settings.max_per_channel} 条"
    )
    print(f"并发：{settings.concurrency}  超时：{settings.timeout}s")

    print("\n结论：" + ("配置可用，可以执行 update" if ok else "存在阻塞项，先按上面的 ✗ 修正"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    # 配置目录为空时补默认值：Docker 挂载空目录后也能直接跑起来
    if args.command in {"update", "serve", "doctor"} and not args.config:
        created = ensure_defaults(root)
        if created:
            print(f"[初始化] 已写入默认配置：{', '.join(created)} → {root / 'config'}")

    if args.command == "update":
        return cmd_update(root, args.config, args.quiet)
    if args.command == "serve":
        return cmd_serve(root, args.config, args.host, args.port)
    if args.command == "doctor":
        return cmd_doctor(root, args.config)
    if args.command == "probe":
        return cmd_probe(root, args.config, args.urls)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
