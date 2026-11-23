> **⛔ 已归档（2026-09-30）——论坛运维线的历史记录（20 轮/R1-R20 已全部交付完毕）。**
> 论坛运维的运行文档见 DEGRADATION_PLAYBOOK.md / GRAFANA_INTEGRATION.md（未归档）；
> 当前项目总览见 [ARCHITECTURE_ROADMAP.md](../ARCHITECTURE_ROADMAP.md)。

# 小秋 Ops Agent · AI 能力深化迭代报告（v3，2026-09-15）

> 目标：把小秋从"带 AI 侧栏的观测台"迭代为"生产级 AI Ops Agent"——充分使用 LangGraph 特性、
> 融入 HITL 人机协同、提供受控执行能力，且每个能力都有业界成熟实践背书与验收。

## 一、业界调研结论（实施依据）

| 实践 | 来源 | 我们的对齐 |
|---|---|---|
| HITL = interrupt → 持久化 → 人审 → resume | [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[HITL middleware](https://docs.langchain.com/oss/python/langchain/human-in-the-loop) | SSE 对话不能挂起等人工数分钟 → **审批落 PG（ops_approvals）跨进程/跨会话可恢复**，`check_action_status` 工具 = resume 等价物 |
| 审批门 + 置信门 + 结构化输出护栏 | [kagent](https://kagent.dev/)（K8s 原生 agent）、[OpenAI 生产级 K8s agent](https://community.openai.com/t/case-study-building-a-production-safe-kubernetes-agent-with-structured-outputs/1380441) | 动作白名单硬边界（服务端 ValueError，非提示词约束）；L3 永不自动 |
| 沙箱执行（受控命令面） | kagent/devcontainers 范式 | **docker 一次性容器**（alpine + host 网络 + 128m/0.5cpu + 10s 超时 + 白名单首词 + 台账）；**不用 k3s**——老师集群是共享设施（Qdrant/RAG），不宜跑探诊负载，自服务器 docker 即"云沙箱"且零新基建 |
| 定时 AI 报告 | Grafana reporting/AIOps 惯例 | 每日 09:07 AI 简报（四源事实→LLM→通知中心，模板兜底永不哑火） |

## 二、本轮交付（全部已部署生产）

### 1. HITL 审批闭环（AI 提议 → 人批准 → 执行 → 回传）
- **`ops_approvals` 表**：module/action/payload/reason(AI 的理由，面板展示)/status 五态机
  （pending→approved→done/failed；或 rejected）
- **Agent 工具**：`request_action(module, action, reason)`（interrupt 形态）+
  `check_action_status(id)`（resume 形态）
- **面板**：告警页新增「审批」tab——待批卡高亮显示 AI 理由，批准执行/拒绝一键；历史含执行结果
- **安全边界**（服务端硬约束，不靠提示词）：agent 仅可提议 `module_log/diagnose/http_probe`；
  `restart_container`（L3）发起即 ValueError；重复处理 404；拒绝也台账；执行失败回写 failed（agent 可见原因）
- **生产 E2E 实测**：AI 调 triage 发现 go_health 无遥测 → 主动 request_action（理由："triage 显示
  窗口内无遥测数据"）→ 回复引导用户去审批 tab → 人工批准 → http_probe 执行 healthy=True →
  状态 done。全链 < 30s。

### 2. 云沙箱（sandbox_exec）
- 一次性 `alpine:3.20` 容器、host 网络（可达内网服务）、128m/0.5cpu、10s 硬超时、输出截断
- **白名单首词**：curl/ping/nc/dig/wget/traceroute/nslookup；rm/sh/psql 一律拒绝（拦截有台账）
- 每次执行（含拒绝）落行动台账审计；本地无 docker → 诚实降级提示

### 3. Agent 工具面 5 → 12（从"查目录"到"真排障"）
新增 `query_traffic`（窗口台账+TOP5）/ `query_errors`（指纹+样本 rid）/ `trace_by_rid`
（Jaeger 下钻）/ `host_status`（CPU/内存/负载/磁盘/容器）——agent 现在能看到**真实遥测**，
不止结构目录。系统提示同步声明动作分级与"绝不声称已执行"红线。

### 4. 每日 AI 运维简报
- APScheduler 09:07（与 digest 09:00 错开）：SLO 燃尽/24h 指纹/主机/流量四源事实 → LLM 摘要 →
  通知中心 `[AI 简报]`；LLM 失败自动模板兜底
- 手动触发：`POST /ops/briefing/run`

## 三、验收与回归
- **i45**（新增四项）：审批生命周期全路径（批准执行 done/拒绝/L3 硬拒/重复拒/列表）、沙箱白名单
  拦截+降级、简报生成进通知中心、12 工具注册与系统提示契约
- 回归：i40（console 五项）/ i26（R7 动作）/ i13（agent 流式）全绿
- 生产：审批 E2E 闭环 ✓（见上）、简报手动触发 ✓

## 四、AI 能力全景（当前终态）

| 层 | 能力 | HITL |
|---|---|---|
| 看板 | 图三镜头/放映诊断/AI 简报卡 | — |
| 诊断 | AI 诊断/AI 自测(http_probe)/四问/LLM 归因/指纹排查/放映 | 人发起 |
| 对话 | 12 工具 ReAct（真实遥测+结构+审批+沙箱），流式+降级梯 | — |
| 行动 | request_action → 审批 tab → 执行 → check_action_status | **机器提议，人批准** |
| 自动 | 告警联动剧本（L1/L2 dry_run）/ 每日 AI 简报 | 剧本 auto 需人配 |

## 五、遗留与下一步（按价值排序）
1. **审批通知推送**：pending 审批目前只在面板可见，可联动邮件/告警通道
2. **多步剧本编排**：把 request_action 级联成"诊断→提议→执行→验证"的 LangGraph 子图（checkpointer 复用主图 PG）
3. **沙箱命令面扩展**：按需加 `mysql`（只读 SELECT）、`redis-cli --stat`
4. k3s 云沙箱：若未来有独立集群资源，可将 alpine 换成常驻 pod 池（当前 docker 按需容器已满足）
