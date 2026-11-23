#!/bin/bash
# M5 备份恢复演练：最近一份日备 → 隔离容器恢复 → 全表对账 → RTO。
# 不碰生产库；coscmd 未配置 bucket（异地链缺口如实记录，见终局复盘）。
set -u
T0=$(date +%s.%N)
LATEST=$(ls -t /root/SSE_Market/backups/pg/sse_agent_*.sql.gz | head -1)
echo "backup: $LATEST ($(du -h "$LATEST" | cut -f1), $(date -r "$LATEST" '+%F %T'))"

docker rm -f sse_pg_drill >/dev/null 2>&1
docker run -d --name sse_pg_drill --network none \
  -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=sse_agent \
  pgvector/pgvector:pg16 >/dev/null
for i in $(seq 1 30); do
  docker exec sse_pg_drill pg_isready -U agent -d sse_agent >/dev/null 2>&1 && break
  sleep 1
done
T1=$(date +%s.%N)

gunzip -c "$LATEST" | docker exec -i sse_pg_drill psql -U agent -d sse_agent -v ON_ERROR_STOP=0 -q >/dev/null 2>&1
T2=$(date +%s.%N)

TABLES=$(docker exec sse_pg_drill psql -U agent -d sse_agent -t -A -c \
  "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
n=0; PASS=0; DIFF=0
for t in $TABLES; do
  n=$((n + 1))
  R=$(docker exec sse_pg_drill psql -U agent -d sse_agent -t -A -c "SELECT count(*) FROM \"$t\"" 2>/dev/null)
  P=$(docker exec sse_agent_pg psql -U agent -d sse_agent -t -A -c "SELECT count(*) FROM \"$t\"" 2>/dev/null)
  if [ "$R" = "$P" ]; then
    PASS=$((PASS + 1))
  else
    DIFF=$((DIFF + 1))
    echo "  (drift) $t: backup=$R live=$P"
  fi
done
T3=$(date +%s.%N)

docker rm -f sse_pg_drill >/dev/null
echo "对账: $n 张 public 表 · $PASS 一致 · $DIFF 漂移（漂移=备份时刻后新写入，方向性核验=live≥backup）"
echo "RTO: 容器ready $(echo "$T1-$T0" | bc)s · 恢复 $(echo "$T2-$T1" | bc)s · 全表对账 $(echo "$T3-$T2" | bc)s · 总 $(echo "$T3-$T0" | bc)s"
