#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '项目运行环境暂不可用，请告诉 AI。不要在终端或聊天中粘贴 Key。'
  exit 1
fi
.venv/bin/python backend/scripts/configure_deepseek_key.py
