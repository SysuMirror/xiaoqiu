# I17 主机守卫 · 部署清单（全部改动目前仅在本地两仓库，线上未动）

执行前确认：Go 仓库（SSE_market_server，online 分支）已有本地未 push 提交 e9ec5a7 + 本次
internal_mail；agent 仓库已有 I16/I17 提交。顺序即依赖顺序，每步含验证与回退。

## 0. 前置（一次性）
```bash
# 生成两把共享密钥（也可沿用现有值）：metrics 采集 token + internal 双向 token
openssl rand -hex 24   # → METRICS_TOKEN
openssl rand -hex 24   # → INTERNAL_TOKEN（agent 与 Go 两侧同值）
```

## 1. Go 后端（新端点 /internal/mail + metrics_guard token 化）
1. push Go 仓库（e9ec5a7 + internal_mail 提交）→ 服务器拉取并重建镜像（部署已切 docker run：
   记录现有 `docker inspect server` 的 run 参数，重建时追加 env：
   `INTERNAL_TOKEN=<INTERNAL_TOKEN> INTERNAL_MAIL_TO=2215020897@qq.com
   OBSERVABILITY_METRICS_TOKEN=<METRICS_TOKEN>`——viper 键 observability.metrics_token
   支持 env 绑定需确认；若不支持则挂配置文件，值同）
2. 验证：`curl -s -H "X-Internal-Token: <INTERNAL_TOKEN>" -d '{"subject":"t","body":"t"}' http://<容器IP>:8080/internal/mail` → `{"code":0,...}`
3. 回退：镜像回滚到 post-music-20260911 tag 重跑

## 2. nginx（/metrics 与 /internal/ 本机-only 路由——公网仍只见 /api/v1/*）
宿主机源文件 /root/SSE_Market/SSE_market_server/nginx_proxy/nginx.conf，容器内 bind mount：
**不能 sed -i，必须 cat > 原地覆盖**，追加两段（upstream 名以现有配置为准）：
```nginx
location = /metrics {          # 采集通道：仅本机（agent host 网络）
    allow 127.0.0.1; deny all;
    proxy_pass http://server:8080/metrics;
}
location /internal/ {          # 运维内部通道（邮件）：仅本机
    allow 127.0.0.1; deny all;
    proxy_pass http://server:8080/internal/;
}
```
验证：`curl -s http://127.0.0.1/metrics -H "Authorization: Bearer <METRICS_TOKEN>" | head -3` 通；
公网 `curl -s https://ssemarket.cn/metrics` → 403。回退：还原 conf 后 restart nginx_proxy 容器。

## 3. 小秋 agent（.env 接线 + 代码更新）
```bash
scp -r app/ config/settings.yaml root@175.178.90.215:/root/SSE_Market/SSE-Agent/sse-agent/
# 服务器 .env 追加：
#   GO_METRICS_TOKEN=<METRICS_TOKEN>     （GO_METRICS_URL 不设——生产走 settings 的 nginx 路由值）
#   INTERNAL_TOKEN=<INTERNAL_TOKEN>
docker restart sse_agent
```
验证：`docker exec sse_agent curl -s http://127.0.0.1:8100/ops/topology | head -c 400` 里
services 带真实 rate；/metrics 出现 host_mem_available_pct 与 docker_container_* 序列。

## 4. host 守卫（替换 memwatch.sh）
```bash
scp scripts/host_memguard.py root@175.178.90.215:/usr/local/bin/  # 或 git 拉取
ssh SSE_Market 'chmod 755 /usr/local/bin/host_memguard.py
cat > /etc/memguard.conf <<EOF
INTERNAL_TOKEN=<INTERNAL_TOKEN>
# 可覆盖：AVAIL_PCT_MIN=8 TOP_PCT_MIN=30 SUSTAIN=3 COOLDOWN_S=1800 TERM_GRACE_S=30
EOF
chmod 600 /etc/memguard.conf
host_memguard.py --selftest'   # 必须输出 SELFTEST PASS
# 先干跑观察 24h（只判定+上报 skipped_whitelist，不 kill）：
ssh SSE_Market 'sed -i "s|^\\* \\* \\* \\* \\*|* * * * *|" /etc/memguard.conf'  # 占位——直接编辑 crontab：
#   crontab -e 里把 memwatch.sh 行替换为：
#   * * * * * DRYRUN=1 /usr/bin/python3 /usr/local/bin/host_memguard.py >> /var/log/memguard.err 2>&1
# 观察期日志/邮件正常后，去掉 DRYRUN=1 转正；旧 memwatch.sh 保留 /usr/local/bin/ 以备回退
```
验证转正：`tail /var/log/memwatch.log`（每分钟一条 avail% 行）；制造测试——低峰期跑一个
吃内存 python（如 `python3 -c "x=[0]*4_000_000_000]"` 需按阈值放大）观察判定与上报。

## 5. 验收（线上）
```bash
curl -s http://127.0.0.1:8100/metrics | grep -E "host_mem|docker_container|ops_incidents"  # 服务器本机
# 手动注入一次 incident 测邮件链路（干跑期内即可）：
curl -s -X POST http://127.0.0.1:8100/internal/ops/incident -H "X-Internal-Token: <INTERNAL_TOKEN>" \
  -d '{"kind":"mem","action":"warning","pid":1,"rss_mb":100,"rss_pct":1.2,"cmdline":"./serve","available_pct":9.9,"total_mb":8084,"available_mb":800,"load1":0.5}'
# → 2215020897@qq.com 应收到 [SSE-Market 守卫] 邮件（含 LLM 或模板快析）
```

## 风险与守门
- Go 重建是唯一动业务的步骤：选低峰；镜像 tag 保留旧版可秒回退
- 守卫默认白名单含 ./serve（Go 后端）与 python run.py（小秋）——最坏情况也只是"只告警不阻断"，
  不会自杀、不会杀主站
- 邮件链路 Go 侧有 1 封/分钟硬限频 + agent 侧上报冷却 15min——不会邮件风暴
