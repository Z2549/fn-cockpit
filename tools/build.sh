#!/bin/bash
# fn-cockpit fpk 构建脚本（CI 与本地通用）
# 只把 fpk 标准成员拷贝到干净目录再调用 fnpack，
# 避免仓库里的 README/LICENSE/tests 等非标文件混入包体。
# 用法：tools/build.sh          （fnpack 需在 PATH，或 FNPACK=/path/to/fnpack）
# 输出：out/fn-cockpit-v<版本>.fpk
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
VER=$(grep -E '^version' "$SRC/manifest" | sed 's/[^0-9.]//g')
[ -n "$VER" ] || { echo "ERROR: cannot read version from manifest" >&2; exit 1; }

FNPACK="${FNPACK:-fnpack}"
command -v "$FNPACK" >/dev/null 2>&1 || { echo "ERROR: fnpack not found (set FNPACK=/path/to/fnpack)" >&2; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/app" "$STAGE/cmd" "$STAGE/config" "$STAGE/wizard"
cp -r "$SRC/app/." "$STAGE/app/"
# 排除 python 编译产物
find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp -r "$SRC/cmd/." "$STAGE/cmd/"
cp -r "$SRC/config/." "$STAGE/config/"
cp -r "$SRC/wizard/." "$STAGE/wizard/" 2>/dev/null || :
cp "$SRC/manifest" "$SRC/ICON.PNG" "$SRC/ICON_256.PNG" "$STAGE/"

(cd "$STAGE" && "$FNPACK" build -d . >/dev/null)

mkdir -p "$SRC/out"
OUT="$SRC/out/fn-cockpit-v${VER}.fpk"
mv "$STAGE/fn-cockpit.fpk" "$OUT"
echo "built: out/fn-cockpit-v${VER}.fpk"
