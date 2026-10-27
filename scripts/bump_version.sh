#!/bin/bash
# =============================================================================
# ShoppingLog 版本号递增（版本号唯一来源：仓库根 VERSION 文件）
#
# 用法:
#   bash scripts/bump_version.sh          递增第三位（patch）0.5.3 -> 0.5.4
#   bash scripts/bump_version.sh patch    同上
#   bash scripts/bump_version.sh minor    递增第二位 0.5.3 -> 0.6.0（需用户明确要求）
#   bash scripts/bump_version.sh major    递增第一位 0.5.3 -> 1.0.0（需用户明确要求）
#
# 约定：每次提交前递增「第三位」；只有当老板明确说要升第二位/第一位时才用 minor/major。
# =============================================================================
set -euo pipefail

SRC="${SHOPLOG_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PART="${1:-patch}"

case "$PART" in
  patch|minor|major) ;;
  *) echo "用法: bash scripts/bump_version.sh [patch|minor|major]" >&2; exit 2 ;;
esac

PY="${SHOPLOG_PY:-$(command -v python3 || echo python3)}"
"$PY" "$SRC/scripts/version.py" "$PART"
