#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
if [[ ! -f data/model-budget.sqlite3 ]]; then
  print '真实验证费用台账尚未准备，请告诉 AI。'
  exit 1
fi
print '真实模型验收入口：http://localhost:18764'
print '本轮费用上限 1 元，最多 20 次请求；额度跨重启保存。'
export MODEL_MODE=model MODEL_BASE_URL=https://api.deepseek.com MODEL_NAME=deepseek-flash
export DATABASE_URL=sqlite:///data/model-smoke.sqlite3 MODEL_BUDGET_PATH=data/model-budget.sqlite3
exec .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 18764 --no-access-log
