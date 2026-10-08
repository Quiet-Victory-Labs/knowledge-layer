#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

python3 scripts/init_env.py
docker compose up --build -d --wait

ui_port="${KNOWLEDGE_UI_PORT:-$(sed -n 's/^KNOWLEDGE_UI_PORT=//p' .env | tail -n 1)}"
ui_port="${ui_port:-4173}"
echo "Connection UI: http://127.0.0.1:${ui_port}"
echo "Agent endpoint: http://127.0.0.1:${ui_port}/agent/mcp/"
