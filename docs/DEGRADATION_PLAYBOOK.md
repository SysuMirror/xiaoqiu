# 降级预案（R19 · 生产稳定性②）

> 原则：任何一层不可用时，下一层接住——面板可用性优先于功能完整性。

## 分层降级链

| 层 | 故障 | 降级行为 | 恢复条件 |
|---|---|---|---|
| L0 PG 不可达 | agent 启动失败 | 容器 restart unless-stopped 自动重试；期间 /health/ready 503 | PG 恢复后 agent 自动连上 |
| L1 Go 后端不可达 | scraper/errtail/probe 全 down | 面板正常渲染但遥测区"无数据/探测中"；AI 对话可用（LLM 走外网） | Go 容器恢复 |
| L2 LLM 不可达 | AI 对话降级 | 流式→单次→前端规则引擎三级降级（I16 实现）；诊断报告走模板快析 | LLM 网络恢复 |
| L3 Jaeger 不可达 | 链路追踪不可用 | /ops/trace 返回 available:false，面板显示"Jaeger 不可达"；不影响其他功能 | Jaeger 容器恢复 |
| L4 docker.sock 不可用 | L3 动作不可用 | 重启动作返回"docker 不可达"；审计落 unavailable | sock 挂载恢复 |
| L5 邮件通道断 | 告警只进面板 | 通知中心仍可用（DB 内）；guard_mail_total{failed} 计数可见 | Go /internal/mail 恢复 |

## 恢复演练记录

| 日期 | 演练项 | RTO | 结论 |
|---|---|---|---|
| 2026-09-14 | PG 全量恢复（R10） | <1min | 四表行数对账一致 ✓ |
| 2026-09-14 | agent 容器重启 | 4-6s | 优雅关停 12 次留痕 ✓ |
| 2026-09-14 | Go 容器重建（I17 部署） | 4s | 自动回滚脚本就绪 ✓ |

## RTO/RPO

- **RPO**：PG 备份每日 4:10 → 最大丢失 24h 台账数据（面板可用性不受影响——实时数据在内存）
- **RTO**：< 5min（docker restart sse_agent_pg + agent 自动重连）
- 改进路径：WAL 归档（RPO→分钟级）→ 留需时可加
