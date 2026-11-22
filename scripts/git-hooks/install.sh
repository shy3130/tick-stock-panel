#!/bin/sh
# 把仓库内的 git hooks 装到 .git/hooks。
# 需要在仓库根目录执行: sh scripts/git-hooks/install.sh
set -e
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
HOOK_SRC="$ROOT/scripts/git-hooks/pre-commit"
HOOK_DST="$ROOT/.git/hooks/pre-commit"

if [ ! -f "$HOOK_SRC" ]; then
    echo "找不到 $HOOK_SRC"
    exit 1
fi

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
echo "已安装 pre-commit -> $HOOK_DST"
echo "验证: python scripts/preflight.py   (或直接提交一次看是否被拦)"
