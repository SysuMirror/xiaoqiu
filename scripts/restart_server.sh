#!/bin/bash
# 按端口精确重启开发服务。用法: bash scripts/restart_server.sh
cd "$(dirname "$0")/.."
PID=$(netstat -ano | grep ':8100' | grep LISTENING | awk '{print $5}' | head -1)
if [ -n "$PID" ]; then
  taskkill //F //PID "$PID" >/dev/null 2>&1
  sleep 2
fi
.venv/Scripts/python run.py > server.log 2>&1 &
for i in $(seq 1 20); do
  sleep 1
  if curl -s -m 2 http://localhost:8100/health/ready | grep -q ok; then
    echo "server ready"; exit 0
  fi
done
echo "server failed to start"; tail -20 server.log; exit 1
