#!/bin/bash
# 开发用 SSH 隧道（自动重连）：PG 5433 + Qdrant 6333 + Embedding 13012 + Go 后端 metrics 8101 + 论坛 MySQL 3506
# metrics 的目标地址是 Go 容器 IP（docker 桥接网络，重建容器会变）——每轮重连前动态解析。
# 3506 走隧道而非直连公网：生产安全组收口后 175.178.90.215:3506 不再可达（本地 settings 用 127.0.0.1:3506）。
while true; do
  GO_IP=$(ssh -o BatchMode=yes -o ConnectTimeout=10 SSE_Market \
    "docker inspect server --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'" 2>/dev/null)
  if [ -z "$GO_IP" ]; then
    echo "[tunnel] go server IP resolve failed, retrying in 10s..."
    sleep 10
    continue
  fi
  echo "[tunnel] go metrics -> $GO_IP:8080"
  ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes \
      -N -L 5433:127.0.0.1:25432 -L 6333:127.0.0.1:25033 -L 13012:127.0.0.1:13035 \
      -L 8101:$GO_IP:8080 -L 3506:127.0.0.1:3506 SSE_Market
  echo "[tunnel] disconnected, retrying in 3s..."
  sleep 3
done
