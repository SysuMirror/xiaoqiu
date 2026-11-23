# 宿主级变更纪律与批准门（M0 固化件）

> **来源**：INC-SSE01（2026-09 三层叠加事故：校园零信任网关劫持出站 443 × k3s 重启连锁 × 容器别名）。
> **性质**：共享生产宿主（腾讯云 4C15G，论坛生产 + k3s 混部）上一切**宿主级操作**的强制流程。
> **上位文档**：[ARCHITECTURE_ROADMAP.md](ARCHITECTURE_ROADMAP.md) §3.3 不变量 ⑤。

---

## 1. 什么算「宿主级变更」（触发本流程）

| 类别 | 例子 |
|---|---|
| 全局服务重启 | `systemctl restart k3s` / `docker` / `nginx`（宿主层）/ `frps` |
| 定时与守护 | crontab 增删改、systemd unit 增删改 |
| 网络路径 | 防火墙/iptables、端口监听变化、DNS/hosts、网关路由 |
| 存储与磁盘 | 磁盘清理、卷删除、分区 |
| 生产容器 | 论坛 nginx_proxy / server(Go) / MySQL 的重建与镜像切换 |

**不算**（qiudock ns 内、平台设计内的工作负载）：qdockd 自身的 deploy/rollout、沙箱 pod 增删、bench 租户 API 调用——按 ROADMAP §3.3 常规变更即可。

## 2. 三步铁律（INC-SSE01 裁决）

1. **基线留档**：操作前抓取端口/进程/路由/服务状态快照，落 `/root/qiudock/baselines/<日期>-<操作名>.txt`。
2. **对照**：操作后同口径复测，逐项 diff，异常即回滚。
3. **报备**：群内通知（做什么/爆炸半径/预计时长/回滚方式），获批后择窗口执行。

## 3. 接手共享宿主五步 checklist（REPLAN §8）

接手任何别人维护的共享机器前，按序回答：

1. **端口**：`ss -tlnp` 全量监听与归属进程清单
2. **进程**：`systemctl list-units --state=running` + 关键 docker 容器及其作者
3. **路由**：出站路径（是否有网关/代理劫持面）、入站路径（公网 DNAT/frp 隧道）
4. **接口**：本机对外提供的全部服务面（谁在调用、凭证在哪）
5. **在册服务**：团队登记的 30+ 项目与本机的关系（哪些只是流量经过，哪些真实落位）

## 4. 批准门模板（每次宿主级变更填一行，append-only）

| 日期 | 操作 | 爆炸半径 | 回滚 | 负责人 | 批准人 | 窗口 | 结果 |
|---|---|---|---|---|---|---|---|
| 2026-09-30 | ~~安装 443 哨兵 cron~~ | — | — | 负责人 | 负责人 | — | **豁免**（根因实为 k3s 抢 docker 流量非网关劫持；指纹续期 toil>价值；判定见 REPLAN §9） |
| 2026-09-30 | M2 混沌：杀 qdockd pod ×3（qiudock ns 内） | 小秋 sandbox_exec 中断 <60s | Deployment 自愈 | 负责人 | 负责人（会话内批准） | 当日 | RTO=3.4s，3 真 bug 修复（p2r4） |

## 5. 首个样例 · 443 哨兵安装 runbook

对应 [scripts/sentinel_443.sh](../scripts/sentinel_443.sh)（脚本本体已在仓库，安装动作走本门）：

```bash
# 1) 基线留档
mkdir -p /root/qiudock/baselines
{ ss -tlnp; crontab -l; } > /root/qiudock/baselines/$(date +%F)-sentinel-install.txt

# 2) 放脚本与配置（指纹从当前真实证书取得后入册）
cp scripts/sentinel_443.sh /root/qiudock/
#    conf：域名+当前指纹；smtp 路由参数从 agent 邮件配置抄录（秘密仍读 .env，不落本文件）
vim /root/qiudock/sentinel-443.conf   # ssemarket.cn <SHA256指纹>；smtp_host=…；smtp_port=465；alert_to=…

# 3) 自测（M0 验收：完整告警链走一遍，邮件标题带 [SELF-TEST]）
/root/qiudock/sentinel_443.sh --self-test

# 4) 上 cron（每 5 分钟）
( crontab -l 2>/dev/null; echo '*/5 * * * * /root/qiudock/sentinel_443.sh >> /root/qiudock/sentinel-443.log 2>&1' ) | crontab -

# 5) 对照：下一轮 cron 后看日志一次指纹匹配记录；crontab -l 比对基线
```

## 6. 既有防线清单（本纪律的机械化部分）

| 防线 | 位置 | 验证 |
|---|---|---|
| deploy 禁触 kube-system 断言 | qiudock 仓库 `qdockd/scripts/guard_manifests.sh`（拆库后随平台走；deploy.sh 每次调用） | `test_deploy_guard.sh` 5 用例 |
| 443 劫持哨兵 | [scripts/sentinel_443.sh](../scripts/sentinel_443.sh)（cron 5min） | `--self-test` 告警链 |
| 集群重装防线 | k3s HelmChartConfig 固化（INC 修复件，已在案） | — |
| 批准门台账 | 本文档 §4 | append-only |
