#!/usr/bin/env python3
"""把仓库里的 __OWNER__ 占位符换成你的 GitHub 用户名。

用法：
    python scripts/set-owner.py            # 从 git remote origin 里自动解析
    python scripts/set-owner.py zhangsan   # 直接指定

通常的发布流程是先把远程地址加好，再跑这个脚本：

    git remote add origin https://github.com/你的用户名/hd-iptv.git
    python scripts/set-owner.py
    git add -A && git commit -m "chore: 填入仓库地址"
    git push -u origin main
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MARKER = "__OWNER__"

# 只改这些类型的文件，避免误伤二进制
SUFFIXES = {".md", ".yml", ".yaml", ".toml", ".txt", ".py", ".example", ".json"}


def owner_from_remote() -> str:
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""

    # https://github.com/owner/repo.git  git@github.com:owner/repo.git
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else ""


def main() -> int:
    root = Path(__file__).resolve().parent.parent

    owner = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not owner:
        owner = owner_from_remote()
    if not owner:
        print("没有拿到用户名。先加远程，或直接传：python scripts/set-owner.py 你的用户名")
        return 1

    changed: list[str] = []
    self_path = Path(__file__).resolve()
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        if ".git" in path.parts:
            continue
        if path.resolve() == self_path:  # 别把脚本自己的 MARKER 常量改掉
            continue
        text = path.read_text(encoding="utf-8")
        if MARKER not in text:
            continue
        path.write_text(text.replace(MARKER, owner), encoding="utf-8")
        changed.append(str(path.relative_to(root)))

    if not changed:
        print(f"没有需要替换的占位符（用户名：{owner}）")
        return 0

    print(f"用户名：{owner}")
    for name in changed:
        print(f"  已更新 {name}")
    print("\n下一步：git add -A && git commit -m \"chore: 填入仓库地址\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
