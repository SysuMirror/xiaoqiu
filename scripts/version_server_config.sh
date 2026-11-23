#!/bin/bash
# R19: 服务器配置版本化——关键配置快照拉回本地 git 管理
# 用法: bash scripts/version_server_config.sh（拉取并 diff 提示）
set -u
DEST=./var/server-config
mkdir -p "$DEST/nginx" "$DEST/systemd" "$DEST/cron"
SSH="${1:-SSE_Market}"
scp -q "$SSH:/root/SSE_Market/nginx_proxy/nginx.conf" "$DEST/nginx/" 2>/dev/null
scp -q "$SSH:/etc/memguard.conf" "$DEST/systemd/" 2>/dev/null
scp -q "$SSH:/root/SSE_Market/scripts/docker-disk-maintenance.sh" "$DEST/systemd/" 2>/dev/null
ssh "$SSH" crontab -l > "$DEST/cron/crontab.txt" 2>/dev/null
echo "配置快照已拉取到 $DEST（git diff 查看变更）"
