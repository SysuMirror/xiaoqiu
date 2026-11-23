#!/usr/bin/env bash
# 本地提交门禁（v3）：改动文件 ruff + 全量编译 + 权限矩阵一致性。
# 用法: bash scripts/ci.sh        （golden eval 走手动：python app/eval/run_eval.py，不打 LLM）
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python.exe
command -v $PY >/dev/null 2>&1 || PY=python3

echo "[ci] 编译检查"
$PY -m compileall -q app tests scripts
echo "[ci] 编译通过"

echo "[ci] ruff（仅本轮改动文件）"
FILES=$(git status --porcelain | awk '$1!="D" && $2 ~ /\.py$/ {print $2}'
        git diff --name-only HEAD --diff-filter=M | grep '\.py$' || true)
if [ -n "${FILES// /}" ]; then
  $PY -m ruff check $FILES 2>/dev/null || $PY -m ruff check $(echo "$FILES" | tr '\n' ' ')
else
  echo "  无改动 py 文件"
fi

echo "[ci] 权限矩阵一致性"
$PY -m pytest tests/test_registry.py -q

echo "[ci] 全部通过"
