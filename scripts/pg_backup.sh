#!/bin/bash
# PG 备份（R10 容灾）：sse_agent 库 → gzip → 7 天滚动。
# 服务器 crontab：10 4 * * * /root/xiaoqiu-build/scripts/pg_backup.sh
set -u
DEST=/root/SSE_Market/backups/pg
mkdir -p "$DEST"
STAMP=$(date +%F_%H%M)
OUT="$DEST/sse_agent_$STAMP.sql.gz"
docker exec sse_agent_pg pg_dump -U agent -d sse_agent | gzip -q > "$OUT"
find "$DEST" -name 'sse_agent_*.sql.gz' -mtime +7 -delete
echo "$(date '+%F %T') backup -> $OUT ($(du -h "$OUT" | cut -f1)); 保留 $(ls "$DEST" | wc -l) 份"

# R19: 异地备份——推腾讯云 COS（失败不阻断本地备份链路）
COS_BUCKET="${COS_BACKUP_BUCKET:-}"
if [ -n "$COS_BUCKET" ]; then
  # coscmd 或 rclone 任一可用即推
  if command -v rclone >/dev/null 2>&1; then
    rclone copy "$OUT" "$COS_BUCKET/pg/" --quiet 2>>/var/log/pg_backup.log && \
      echo "$(date '+%F %T') offsite -> cos OK" >> /var/log/pg_backup.log
  elif command -v coscmd >/dev/null 2>&1; then
    coscmd upload "$OUT" pg/ >> /var/log/pg_backup.log 2>&1
  else
    echo "$(date '+%F %T') offsite SKIP: no rclone/coscmd" >> /var/log/pg_backup.log
  fi
fi
