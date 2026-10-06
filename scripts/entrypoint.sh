#!/usr/bin/env bash
# 容器入口：启动 API，并在启动后执行一次排程审计冒烟调用，
# 确认“初始化”完成（天文库加载、端到端审计可调用）。
set -euo pipefail

PORT="${PORT:-8080}"

echo "[entrypoint] starting night-schedule-audit on :${PORT}"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" &
APP_PID=$!

# 等待健康检查通过
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "[entrypoint] service healthy after ${i} probe(s)"
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[entrypoint] service failed to become healthy" >&2
    kill "$APP_PID" 2>/dev/null || true
    exit 1
  fi
done

# 初始化冒烟：一次真实审计调用
echo "[entrypoint] running initialization smoke audit ..."
SMOKE=$(curl -fsS -X POST "http://127.0.0.1:${PORT}/api/v1/audit" \
  -H 'Content-Type: application/json' \
  --data @examples/success_m31.json)
echo "$SMOKE" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d["accepted"] is True
print(f"[entrypoint] smoke audit OK: {d[\"totals\"][\"frame_count\"]} frames, "
      f"last frame {d[\"last_frame\"][\"status\"]}, "
      f"margin {d[\"last_frame\"][\"remaining_window_seconds\"]:.1f}s")
'

# 转发终止信号，保证容器可优雅停止
trap 'kill -TERM "$APP_PID" 2>/dev/null || true' TERM INT
wait "$APP_PID"
