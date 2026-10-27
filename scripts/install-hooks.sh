#!/bin/bash
# 安装 git 钩子（pre-commit：每次提交自动递增第三位版本号）
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS="$(git -C "$SRC" rev-parse --git-path hooks)"
mkdir -p "$HOOKS"
cp "$SRC/scripts/hooks/pre-commit" "$HOOKS/pre-commit"
chmod +x "$HOOKS/pre-commit"
echo "已安装: $HOOKS/pre-commit"
