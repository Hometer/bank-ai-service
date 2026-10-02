#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, sqlalchemy, alembic, httpx, dotenv, uvicorn' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r backend/requirements.txt
fi
print '本机文字演示启动后，请在浏览器打开 http://127.0.0.1:18763'
print '本次强制规则演示模式，不调用付费模型。按 Control+C 停止服务。'
MODEL_MODE=demo .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 18763 --no-access-log
