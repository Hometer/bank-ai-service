#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '项目隔离环境缺失，请把此提示交给 AI 检查。'
  exit 1
fi
print '语音与 DeepSeek 客服：http://127.0.0.1:18769'
print '本轮另批最多1元/50次HTTP请求（含开发实测/重试）；原费用记录保留。'
print '仅限非敏感演示问题，语音须主动开启；人工参考为草稿，未连接银行坐席。'
print '保持终端打开，结束时按 Control+C。'
ASR_MODEL_FILE="${LOCAL_ASR_MODEL:-$PWD/data/models/ggml-large-v3-turbo-q5_0.bin}"
if [[ ! -f "$ASR_MODEL_FILE" && -z "${LOCAL_ASR_MODEL:-}" ]]; then
  ASR_MODEL_FILE="$PWD/data/models/ggml-small.bin"
fi
V15_ENABLED=1 LOCAL_ASR_ENABLED=1 LOCAL_ASR_MODEL="$ASR_MODEL_FILE" PUBLIC_KNOWLEDGE_ENABLED=1 MODEL_MODE=demo DATABASE_URL="sqlite:///$PWD/data/voice-preview.sqlite3" .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 18769 --no-access-log
