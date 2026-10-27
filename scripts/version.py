#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ShoppingLog 版本号单一来源。

版本号写在仓库根目录的 `VERSION` 文件里，全项目（HTTP API /api/health、
MCP `initialize` 的 serverInfo、网页右下角徽标、deploy 校验）统一从这里读，
不再各处硬编码，避免"线上跑的是哪个版本"对不上。

用法:
  python3 scripts/version.py              打印当前版本
  python3 scripts/version.py show         同上
  python3 scripts/version.py patch        递增第三位 0.5.3 -> 0.5.4（每次提交默认做这个）
  python3 scripts/version.py minor        递增第二位 0.5.3 -> 0.6.0（需用户明确要求）
  python3 scripts/version.py major        递增第一位 0.5.3 -> 1.0.0（需用户明确要求）
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
FALLBACK = "0.0.0"

RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_raw():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return FALLBACK


def get_version():
    """当前版本号；VERSION 缺失或格式异常时回退到 FALLBACK。"""
    raw = read_raw()
    return raw if RE.match(raw) else FALLBACK


def bump(part="patch"):
    """递增版本号并写回 VERSION 文件，返回 (旧版本, 新版本)。"""
    old = get_version()
    a, b, c = (int(x) for x in RE.match(old).groups())
    if part == "major":
        a, b, c = a + 1, 0, 0
    elif part == "minor":
        b, c = b + 1, 0
    elif part == "patch":
        c = c + 1
    else:
        raise ValueError("part 只能是 major / minor / patch")
    new = f"{a}.{b}.{c}"
    VERSION_FILE.write_text(new + "\n", encoding="utf-8")
    return old, new


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "show"
    if arg == "show":
        print(get_version())
        return 0
    if arg not in ("major", "minor", "patch"):
        print("用法: python3 scripts/version.py [show|patch|minor|major]", file=sys.stderr)
        return 2
    old, new = bump(arg)
    print(f"{old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
