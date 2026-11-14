#!/usr/bin/env bash
# 公众号文章一键同步：自举 venv -> 增量抓取 -> 重建预览 -> 提交推送
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[sync.sh] 初始化 Python 虚拟环境..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r tools/requirements.txt
fi

exec .venv/bin/python tools/sync.py "$@"
