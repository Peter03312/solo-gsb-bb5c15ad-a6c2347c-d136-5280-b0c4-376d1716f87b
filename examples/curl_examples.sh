#!/usr/bin/env bash
# 排程审计 curl 调用示例
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:8080}"

echo "== 健康检查 =="
curl -fsS "${BASE}/healthz" | python3 -m json.tool

echo
echo "== 成功：M31，V 3x120s + R 2x60s，读出10s，V->R 切换25s =="
curl -fsS -X POST "${BASE}/api/v1/audit" \
  -H 'Content-Type: application/json' \
  --data @examples/success_m31.json | python3 -m json.tool

echo
echo "== 拒绝：目标在暗夜空中低于最低高度（HTTP 422）=="
curl -sS -X POST "${BASE}/api/v1/audit" \
  -H 'Content-Type: application/json' \
  --data @examples/reject_not_visible.json | python3 -m json.tool

echo
echo "== 拒绝：同一目标 A 的重复曝光不连续（HTTP 422）=="
curl -sS -X POST "${BASE}/api/v1/audit" \
  -H 'Content-Type: application/json' \
  --data @examples/reject_interleaved.json | python3 -m json.tool
