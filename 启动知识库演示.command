#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '项目隔离环境缺失，请把此提示交给 AI 检查。'
  exit 1
fi
print '东亚银行香港知识库优先：http://127.0.0.1:18768'
print '仅限非敏感公开问题；本入口不调用模型、不执行银行业务。保持终端打开，结束时按 Control+C。'
PUBLIC_KNOWLEDGE_ENABLED=1 MODEL_MODE=demo DATABASE_URL="sqlite:///$PWD/data/knowledge-preview.sqlite3" .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 18768 --no-access-log
