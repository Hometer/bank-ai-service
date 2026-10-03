#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '项目隔离环境缺失，请把此提示交给 AI 检查。'
  exit 1
fi
print 'V1.4 规则演示：http://127.0.0.1:18765'
print '仅使用非敏感演示问题；此入口不会调用真实模型。保持此终端打开，结束时按 Control+C。'
MODEL_MODE=demo DATABASE_URL="sqlite:///$PWD/data/v14-preview.sqlite3" .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 18765 --no-access-log
