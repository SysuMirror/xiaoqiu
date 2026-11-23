# SSE-Agent（小秋 v2）架构文档

> 状态：基准版 v1 · 2026-09-02
> 定位：秋招项目 · 单服务器 · 1-2 人开发 · MVP 先行
> 原则：该复杂时展现工程，该简单时展现取舍；每个风险必须显式声明为「接受+理由」或「记录+触发条件」

---

## 1. 项目定位

一个统一入口的网站智能 Agent（小秋 v2）：

- **对话域**：论坛问答（SQL 模板 + RAG）、知识库检索、平台状态问答，SSE 流式
- **运维域**：集市服务/进程监控、杀进程（高危审批）、定时任务对话管控、事故报告
- **主动域**：每日日报、告警响应、自动回帖（phase 2）
- **底座**：RBAC 权限、三层记忆、多模型池、邮件通知、可观测、评测

对标业界成熟实现，生产级 MVP：功能收敛，但可靠性/安全性/可维护性设计完整。

## 2. 约束（所有决策的前提）

| 约束 | 内容 |
|---|---|
| 规模 | 秋招展示项目，1-2 人开发，单台服务器（175.178.90.215，docker compose） |
| 部署 | 自部署 FastAPI + SSE，不用 LangGraph Platform |
| 现有设施 | 论坛 Go 后端 + MySQL、iWiki、Prometheus/Grafana/Jaeger、nginx_proxy、dashscope OpenAI 兼容网关 |
| 兼容 | 与小秋 v1（langgraph 版）功能覆盖关系，但不继承代码，全部重写 |
| 通讯 | 邮件为主通知通道 |

## 3. 总体架构

```
用户/前端 ── SSE/HTTP + JWT(unisso) ── 邮件（审批/日报/告警）
   │
   ▼
FastAPI 接入层
  auth(JWT→角色) → 限流(进程内,按user_id) → request_id → 创建 run
  端点: /task/sse  /task  /task/resume  /task/result  /admin/*  /health/*
   │
   ▼
LangGraph 单图（按角色编译并缓存）
  prep(记忆注入+台账摘要+并行RAG) → router
    ├ chat_domain: 论坛SQL模板工具 / KB检索 / 平台状态 / 闲聊 / 联网搜索(开关) / 用户反馈直达
    ├ chat_domain(v3写侧): create_post/create_comment(L3·用户自操作HITL) / toggle_like/vote(L2可逆)
    └ ops_domain:  kill_process(L3·admin运维HITL) / 任务管控(phase2)
  → hitl(interrupt) → output(清洗+postID→链接+脱敏)
   │
   ▼
PostgreSQL(+pgvector) —— agent 全部状态的唯一归属
  checkpointer / 记忆store / 行动台账 / 审计 / runs / ingest占位表
   │
   ├─ APScheduler(进程内): daily_digest（MVP 唯一主动任务）
   └─ SMTP: 模板化邮件（日报/审批请求，去重窗口10min）
   └─ Prometheus /metrics + 结构化日志(trace_id) + LangSmith(dev开/prod关)
```

**持久化核心决策**：一个 PostgreSQL（含 pgvector）承载 agent 全部状态。官方组件（langgraph-checkpoint-postgres 的 AsyncPostgresSaver / PostgresStore）、单一备份点、单一故障域、不碰论坛在线 MySQL。**不引入 Redis、不引入 Qdrant**。

## 4. Run 生命周期（可靠性核心）

```
run 状态机: created → running → succeeded | failed | interrupted | cancelled | expired
```

- run 是持久实体（runs 表），`POST /task` 创建即返回 run_id；**SSE 只是观察通道，不是执行通道**
- 断连：起 90s 宽限计时 → 重连取消计时 → 超时 cancel run（结果此前若已产出则可拉取）
- 恢复：前端按 thread + run_id 调 `/task/result` 拉最终结果（不做过程回放，理由见 ADR-4）
- 服务重启：启动扫描所有 running 状态 run → 标记 `failed(reason=server_restart)`（扫描幂等）
- 优雅停机：SIGTERM → 摘流量 → drain 30s → 退出
- 每用户并发 run 上限 2；单 run 预算：recursion_limit=25 + token 上限（超限中断+记账）
- 连接管理：asyncpg pool=10，checkpoint 写完即还连接，SSE 事件走内存 queue

## 5. 记忆系统（三层）

| 层 | 存储 | 内容 | 治理 |
|---|---|---|---|
| L1 会话 | AsyncPostgresSaver | thread 内消息 | thread 空闲 7 天归档（删 checkpoint，摘要进 L3）；长对话 token>12k 触发摘要压缩节点（替换最老一半消息为 summary） |
| L2 行动台账 | PG `actions` 表，append-only，按月分区留 6 个月 | 所有 L2/L3 工具调用 + 所有主动 job 结果（actor/action_type/目标/参数/结果/关联帖/时间/job_id） | 只增不改；对话时 prep 注入「近期自主行动」摘要；提供 `list_recent_actions` 工具 |
| L3 语义记忆 | PostgresStore，namespace=(user_id,"memories") | 用户偏好、习惯 | **写时合并**：新记忆先语义检索 top-k，LLM 判定 merge/update/new，判定失败降级直接 append；**容量 50 条** LRU（last_used_at）；**TTL 90 天**，被使用续期；内容 hash 去重 |

对话表现：用户问「你最近干了什么」→ prep 注入台账摘要 → LLM 组织回答（含链接、待审批项）。
行动台账同时是审计日志：一份表两个用途。
thread 归档摘要写入 L3 受限流（每 thread 1 条），计入容量与 LRU（见 ADR-5）。

## 6. RAG 系统

```
索引: 多源统一文档模型(id/source/title/content/updated_at/perm)
      source ∈ {forum_post, forum_comment, iwiki_doc, user_upload}
      向量库: Qdrant（教学平台 k3s 集群，frp 暴露 ssemarket.cn:25033；组前缀 g3dfc5d61_；
              dev 经 SSH 隧道，服务器本机直连 127.0.0.1:25033）
      embedding: bge-m3（1024 维，GPU，OpenAI 兼容 /v1/embeddings，服务器 13011）
      同步: 论坛帖子按 post_time 增量（Qdrant payload range index + order_by），
            排除私密帖；ingest 服务(phase 3)负责 iWiki/上传写入
检索: 纯向量检索（cosine）；BM25 混检暂缓——eval 量化后不足再上
权限: 检索时按用户角色过滤 source/perm
引用: 答案强制带来源链接；无来源 → 明说「知识库没有」，禁止编造
降级: Qdrant/embedding 不可用 → kb_search 30s 超时优雅降级，
      agent 自动改用关键词搜索兜底（eval 已验证该自愈路径）
```

## 7. 图与工具层（解耦核心）

- **域注册**：`{domain_name → node_builder}` dict，加域 = 新文件 + 注册一行
- **工具注册表**：单文件纯 dict（不做散落装饰器副作用），每工具声明 `(domain, risk, min_role, audit)`
- **`build_graph(role)`** 按 registry 过滤注入工具集，图实例按角色缓存
- **风险分级**：L1 只读 / L2 副作用可撤 / L3 必审。L3 两族：ops 域 admin 运维审批（kill_process）；chat 域用户自操作审批（v3 论坛写侧 create_post/create_comment——审批人=执行人=JWT 用户，无越权面）
- **RBAC**：MVP 两档 user/admin（ops 预留 schema）。防线三层：工具不注入（未授权工具不存在于上下文）→ 工具内参数校验 → L3 强制 interrupt
- **身份铁律**：user_id/role 从 JWT 解出经 `config["configurable"]` 注入，**永不经 LLM**——假设 LLM 会被提示注入，权限不依赖 LLM 自觉；不装 prompt 注入检测器
- **审批契约**：interrupt 卡片渲染**结构化工具参数原文**（`{tool, args, risk}`），不是 LLM 的自然语言复述
- **一致性测试**：一条测试断言「文档权限矩阵 == 代码注册表」，防漂移
- **隐私基线（数据层强制，非 prompt 约束）**：①私密帖（is_private=1）对所有角色一律不可见——admin 也不放开，agent 不是审核工具，看私密帖走论坛本人入口；②匿名帖作者显示「匿名」而非哈希（哈希可跨帖关联分析）；③只 SELECT 列白名单，不触碰 photos/cd_keys 等敏感列；④RAG 索引同步排除私密帖。验收：tests/privacy_check.py（含私密帖标题关键词搜索的强阳性对照）

## 8. 运维域安全设计（kill_process）

- 目标标识 = **docker 容器名白名单**（compose 服务名），**禁止裸 PID**（PID 回收 TOCTOU）；白名单强制排除 agent 自身容器
- 只读展示可带当前 PID，操作只认容器名
- 信任边界声明（ADR-6）：agent 容器持 docker.sock ≈ root 权限，MVP 接受；缓解 = 白名单 + L3 审批 + 审计；彻底解（独立 executor 沙箱）超出 MVP，记录方向
- 链路：权限 → 白名单 → interrupt 人工确认（回显将执行的真实参数）→ 执行 → 台账
- 台账写入为 best-effort + 失败告警（不做 outbox 强一致，见 ADR-7）

## 9. 主动任务域

```
APScheduler(进程内) → job = build_graph(system_identity) + 专属工具集
MVP: daily_digest（汇总→日报→邮件+台账）
幂等: 执行前查台账「今天是否已做」+ misfire_grace_time
失败: 重试 3 次×5min → 终态失败邮件告警 admin
时区: 全链路 Asia/Shanghai，DB 字段 timestamptz
phase 2 扩展: auto_reply（含台账+语义记忆闭环）、unanswered_rescue、alert_playbook
```

## 10. 邮件通道

- 统一 `MailPort` 接口 + Jinja2 模板（**autoescape 强制开启**，不渲染 raw HTML）
- MVP 模板：日报、审批请求；收件人 = 组别名白名单，不投递任意地址
- 反馈直达（I8）：submit_feedback（L2）→ LLM 整理 分类/标题/摘要+保留原话 → feedback 组（负责人邮箱），每用户 1 条/小时；SMTP 未配置（log 模式）时如实告知"已记录"
- 联网搜索（I8）：web_search（L1，配置开关控制注入与提示词——禁用时提示词明说无法联网，防模型幻觉调用不存在的工具），provider 配置驱动（默认百度千帆 AI-Search，免费 1500 次/月按天发放；备选 Tavily 免费 1000 次/月），key 未配置时开关保持关；单用户 10 次/日 + 全局 50 次/日（对齐免费额度）
- 去重窗口：同类事件 10 分钟合并一封（防轰炸）

## 11. 模型层

- OpenAI 兼容网关（服务器已有 dashscope），自写模型池约 200 行
- MVP：主备两端点；单调用 retry 3 次指数退避 → 端点切换 → 连续 N 次失败熔断 60s
- LLM 故障 = 降级运行（不进 readiness，全站不下线）；所有错误映射用户可读中文 + trace_id
- 多 provider 配置表切换；智能路由（按任务分档用便宜模型）phase 2

## 12. 可观测与评测

- Prometheus `/metrics` + 结构化日志（trace_id 贯穿 SSE 全链路）+ Grafana 面板
- LangSmith tracing：**dev 默认开，prod 默认关**（数据私有，排查时手动开，见 ADR-8）
- health 分离：`/health/live`（进程）/ `/health/ready`（DB ping），nginx 按 ready 摘除
- 评测：`eval/` golden cases（问题+期望要点）+ LLM-as-judge 打分，**手动触发，不进 CI**，报告写 JSON 看趋势；事故报告样本 = 手写 `INCIDENT-2026-08-30.md`

## 13. 生产级 Checklist

- run 持久化 + 启动对账孤儿 + 并发上限 + token 预算
- 优雅停机（30s drain）、healthcheck 分离、docker restart policy
- LLM retry/回退/熔断/降级路径（RAG 挂 → SQL 模板回答 + 明示）
- thread TTL 归档、长对话压缩、记忆容量治理（合并/LRU/TTL）
- 每日 pg_dump（RPO 24h，ADR-3）、台账月分区
- nginx SSE 配置：`X-Accel-Buffering: no` + 15s 心跳帧 + `proxy_read_timeout` 调高
- SQL 工具：keyword 通配符转义 + `statement_timeout=3s` + LIMIT 不可被 LLM 改
- 配置单源：config.yaml 为行为配置唯一来源，env 只放 secrets；全链路时区锁定
- GitHub Actions：lint + pytest（工具 mock、图逻辑单测、权限矩阵一致性）
- 备份演练文档化；docker.sock 风险写入部署文档

## 14. MVP 边界

| 做（生产级完整） | 不做但留接口 |
|---|---|
| SSE 对话 + 断连恢复（run 模型） | auto_reply / rescue（job 框架就绪，phase 2） |
| 论坛查询（SQL 模板）+ KB 检索问答 | kb-ingestor 独立服务（phase 3，`ingest_jobs` 表结构即契约） |
| 行动台账 + 三层记忆（治理完整） | 邮件审批链接（phase 2，MVP 审批走前端） |
| kill_process 全链路 HITL | 任务对话管控全量（phase 2，MVP 只读查询） |
| daily_digest + 邮件 | BM25 混检 / Qdrant 迁移（eval 证明需要再做） |
| 评测脚手架（5 golden cases） | ops 角色细分、多模型智能路由、事故报告全流程 |

## 15. 演进路线（每步不推翻前一步）

1. **MVP**（本文档）
2. 主动任务扩展：auto_reply + rescue + 告警 playbook（复用 job 框架与台账）
3. kb-ingestor 独立服务（消费占位表；上传→解析→分块→提取→与 iWiki 合并→审校→发布，状态机 uploaded→processing→pending_review→published/failed）
4. 评测驱动调优（扩 cases，数据决定混检/向量库迁移）
5. 多实例（触发条件明确：才引入分布式 run 调度、网关限流、SSE 粘性会话）

## 16. ADR（架构决策记录）

**ADR-1 单 PostgreSQL 承载全部状态**
官方 checkpointer/store 一级支持 PG；单备份点、单故障域；不碰论坛在线 MySQL。代价：引入一个新容器。替代方案（复用 MySQL）被否：官方无 MySQL checkpointer，社区包不赌生产。

**ADR-2 不用 Redis / Qdrant（MVP）——已部分修订（2026-09-10）**
原决策：单实例下进程内限流与内存 queue 足够；pgvector 满足 MVP 数据量。**修订**：拿到教学平台提供的 Qdrant（k3s + frp 隧道）与 GPU embedding 服务（bge-m3）后，向量层迁移至 Qdrant，解决了本机 embedding 模型不可用的问题——印证了"embedding 服务化"的原方向判断。Redis 维持不用（触发回迁条件：多实例、向量规模、混检质量数据）。

**ADR-3 RPO 24h（每日 pg_dump）**
数据为可再生对话状态非交易数据。生产级做法是 WAL 归档，记录方向。

**ADR-4 不做断连过程回放**
结果无损落库，只丢过程动画；回放需事件日志，价值/成本比低。记录方向。

**ADR-5 checkpoint 无跨版本 schema 迁移承诺**
thread 7 天 TTL 使旧格式自然过期；摘要消息带 version 标记便于识别。记忆与台账 schema 正常演进（有迁移）。

**ADR-6 docker.sock 信任边界**
agent 容器持 docker.sock ≈ root。接受理由：单服务器、自用运维工具、白名单+审批+审计缓解。彻底解：独立低权限 executor 服务，记录方向。

**ADR-7 台账 best-effort 不做 outbox**
现实世界操作（kill）不可回滚，审计缺失降级为 log+邮件告警。强一致审计超出本项目规模。

**ADR-8 LangSmith 默认关闭（prod）**
与「自部署数据私有」叙事一致；langchain 原生 env 开关，排查时临时开。

**ADR-9 自研记忆而非 Zep/MemGPT**
需求为用户偏好级（非文档级），自研 ≈200 行可测可控；写时合并+LRU+TTL 三机制覆盖主要记忆质量问题，且每个决策可一句话讲清。

**ADR-10 审批展示真实参数而非 LLM 复述**
防「嘴上说杀 A 实际传 B」。审批卡片数据 = `{tool, args(JSON), risk}` 原文。

## 17. 已砍决策（防止膨胀的墓碑）

reaper 定时任务 · WAL 归档 · 断点续传 · prompt 注入检测器 · outbox 审计 · eval 进 CI/夜间调度 · 插件化域加载 · Redis · Qdrant(MVP) · Specialist 专家层 · monorepo/packages · kb-ingestor(MVP) · 夜间 eval · prod 默认 LangSmith · 裸 PID 操作

## 18. 记录方向（未排期）

流式回放 · 双控审批 · executor 沙箱 · AG-UI 协议 · WAL 归档 · 邮件审批链接 · 多模型智能路由 · iWiki 写入全流程（待接口）
