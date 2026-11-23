# 小秋 Ops Agent · 图-代码耦合可视化 —— 需求梳理与调研报告

> 状态：v1 调研稿 · 2026-09-10
> 输入：用户 4 点诉求 + 底层愿景（自然语言→领域建模→代码→逆向回设计，图与代码实时耦合）
> 产出：本报告 + 预览图 `ops_dashboard_preview.html`
> 调研方式：8 项定向检索（2026-09-10，官方一手源为主）+ 本项目生产数据交叉验证

---

## 0. 一页结论

你们想做的不是"画图工具"，而是一个**三层的闭环系统**：

```
事实层（代码 + 运行遥测）  ←——唯一的事实源——
        │  投影（自动、单向）                ↖ 意图写入（agent 桥接）
        ▼                                        │
投影层（拓扑图：节点=模块，边=请求流，             │
        叠加 错误定位/流量热度/延迟）              │
        ▲                                        │
        │  标注（bug 卡片、优化请求）              │
意图层（用户诉求 → 小秋 → Ops Agent → 开发者邮件）—┘
```

**三个关键裁决**（调研支撑，见 §3）：

1. **不做双向同步引擎**——UML round-trip/MDD 是 30 年坟场（IBM 2006 经典论文 + InfoQ 复盘）。做**单向投影（代码+绑定清单→图）+ Agent 做桥**（意图→结构化工单→模块定位；代码变更→图自动重投影）。
2. **图不是事实源，绑定清单才是**——你们的 `TOOL_REGISTRY` 权限矩阵 + LangGraph 显式图结构 + nginx 路由**已经是绑定层的 80%**，只差把它导出成一个 `modules.yaml` 并加遥测标签。
3. **可视化必须绑在痛点上**——CodeSee（融资 $10M 的代码可视化独角兽）2024 年倒闭，死因定论："维生素不是止痛药"。你们的止痛药已经存在：**runs 表 23% 失败率的真实数据**。错误定位在图上 = 先有痛，炫酷是结果。

**创新叙事（面试弹药）**：把微服务世界已验证的 Service Graph 模式（Kiali/Grafana Tempo：请求率/错误率/延迟落在图上）**下沉到 LLM Agent 的节点级**（图节点/工具/外部依赖），再叠加业界没有的**意图层**（优化请求标注在模块上）。一句话："Node-level Service Graph with Intent Annotations for LLM Agents"。

---

## 1. 需求梳理

### 1.1 你们原话 → 能力模型

| # | 原话诉求 | 重构后的能力 | 数据源（你们已有/待建） |
|---|---|---|---|
| 1 | "代码分块，怎么分我们也不懂" | **模块化绑定层**：把代码按架构单元切成有身份的模块 | 已有：TOOL_REGISTRY / graph/build.py / nginx 路由 → 待建：modules.yaml |
| 2 | "流程图能绘制并与代码块绑定，AI 能理解对应关系，炫酷可视化" | **投影引擎**：绑定层+代码事实 → 自动渲染拓扑图；LLM 可查询"这个节点对应哪些代码" | 待建：/ops/topology 导出 + 渲染页 |
| 3 | "用户告诉小秋要做优化 → 小秋回'收到' → 异步告知 ops agent → 邮件开发者" | **意图通道**：自然语言 → 结构化优化工单 → 模块定位 → 邮件闭环 | 已有：submit_feedback 管道（I8）/ mail 白名单 → 待建：优化工单类型+devs 收件组 |
| 4 | "运维 agent 监控到 bug 时初定位代码和模块，并在图上显示" | **错误定位投影**：失败聚类 → 模块映射 → 图上红点+卡片 | 已有：runs 表（error 字段）/ actions 台账 / metrics → 待建：错误签名映射表 + 聚类 job |
| 5 | "高频失败请求在对应模块显示" | **RED 指标下沉节点**：Rate/Error/Duration per module | 已有：metrics.py 框架 → 待建：per-tool/per-node 打标签 |
| 6 | "请求流向和频繁程度可见" | **流量边**：边宽=请求量、边色=错误率、动画=活跃流 | 同上 + trace 关联 |
| 7 | "图和代码实时更新（双向）" | **单向投影 + Agent 桥**（替代双向同步，见裁决 1） | 待建：绑定层 CI 一致性检查（fitness function） |

### 1.2 领域模型：Ops Agent 在三层之间搬运什么

```
Ops Agent 的四个职责（复用你们已有的 agent 框架，不引入新基础设施）：
  ① 采集器  —— metrics 加标签、runs.error 聚类（APScheduler job，已有调度器）
  ② 投影器  —— modules.yaml × 遥测 → /ops/topology JSON（图的前后端契约）
  ③ 初判官  —— 错误签名规则（确定性）+ LLM 归因（给嫌疑模块+代码位置，措辞用
               "贡献因素"而非"根因"——Rootly 的教训，见 §2.4）
  ④ 意图中枢 —— 接收小秋转来的优化工单 → 分类/定位模块 → 图上标注 + 邮件开发者
```

### 1.3 "自然语言→建模→代码→逆向回设计"的正确打开方式

这条循环是真实可做的，但**闭环的连接件是 Agent，不是同步引擎**：

- **自然语言→建模**：用户诉求经小秋整理为结构化工单（分类/标题/摘要/原话——I8 已实现同款模式）。
- **建模→代码**：Ops Agent（或开发会话里的 coding agent）拿着工单+绑定清单改代码——人审或 HITL 审批。
- **代码→设计（逆向）**：绑定清单驱动自动重投影；DeepWiki 式 LLM 提取做增量校对。
- **防漂移**：CI 里跑"绑定清单 vs 代码事实"一致性断言（fitness function，你们 `test_registry.py` 权限矩阵一致性测试**就是现成的同款范式**，扩展它即可）。

---

## 2. 市场与前沿调研（检索日 2026-09-10）

### 2.1 图-代码耦合工具线

| 产品 | 模式 | 对我们的启示 |
|---|---|---|
| **LikeC4**（likec4.dev） | Architecture as Code：DSL 单一事实源 → 图；VS Code 实时预览；2026 趋势是"给 AI 读的架构文档" | DSL/清单做事实源、图是投影——印证裁决 2；其"模型机器可读"方向与我们 modules.yaml 一致 |
| **DeepWiki**（Cognition/deepwiki.com） | LLM 把任意代码库自动变成 wiki+架构图+可问答知识图谱；有 Claude Code `/graphify` skill；开源克隆 deepwiki-rs 能推断架构并产 C4 Mermaid | 证明"代码→图"的 LLM 逆向已产品化；适合做我们绑定层的**冷启动种子生成器** |
| **CodeSee**（已死） | 代码地图+onboarding 导览，融资 ~$10M，2024-02 停运，5 月被 GitKraken 收编 | 纯可视化是"维生素"——必须绑痛点（事故/遥测/工单）才有留存。我们的痛点现成：runs 23% 失败 |

### 2.2 双向同步坟场线（为什么裁决 1 成立）

- **UML round-trip / MDD 失败史**：同一信息存在两份工件必有漂移（Wikipedia: Round-trip engineering）；IBM Systems Journal 经典（Hailpern & Tarr 2006）指出最坏情形是**低层（代码）变更向上回传模型**——恰恰是你们想要的"代码逆向到设计"方向；InfoQ《8 Reasons Why MDE Fails》总结：生成代码质量差、开发者直接改代码、模型必然过期。
- **活下来的替代品**：Fitness Functions / ArchUnit 式架构测试（ThoughtWorks、Building Evolutionary Architectures）——**用 CI 断言防漂移，而不是同步引擎**。业界共识（r/softwarearchitecture 讨论）：文档/图第 2 年必烂，除非一致性检查进流水线。
- 结论：**"图随代码走"单向投影 + CI 一致性断言 + Agent 做意图桥**，这是 2026 年的正解姿势。

### 2.3 运行时拓扑线（你们诉求 5/6 的成熟原型）

- **OpenTelemetry servicegraph connector → Prometheus → Grafana Node Graph**：从 trace 自动生成服务图，**节点/边上挂 RED 指标（请求率/错误率/延迟）**——Grafana Tempo 官方 Service Graph 视图就是"边=请求流，粗细/颜色=指标"。这正是你们"请求流向+频繁程度+失败显示"的已验证模式。
- **Kiali（Istio）**：服务网格拓扑+流量动画+错误高亮，交互范式可借鉴。
- **LangSmith**：LangGraph 原生节点级 trace（每步/每工具/每 LLM 调用），失败可重放定位到节点——但它是**时间线视图**，不是叠加意图/自定义拓扑的作战图；且你们 ADR-8 生产默认关（数据私有）。
- **空白点**：以上全部工作在"服务级"或"trace 时间线"，**没有人做"Agent 节点级拓扑 + 意图标注"**——这就是你们的位置。

### 2.4 AI 运维（AIOps）线（诉求 3/4 的大厂姿势）

| 产品 | 做法 | 借鉴点 |
|---|---|---|
| **Sentry Seer** | 应用级 RCA + Autofix（从 issue 上下文自动出修复 PR） | "定位→给嫌疑代码位置→（远期）出修复建议"的分段式路线 |
| **incident.io AI SRE** | 自动时间线/进展通报/相似历史事件召回，宣称覆盖 80% 事件任务；发布过 RCA 的 precision/recall 评测方法 | 对 AI 归因要**量化评测**（我们 eval 框架可直接复用） |
| **Rootly AI** | 明确选择"映射贡献因素，不过早猜根因" | 初定位话术用"嫌疑模块/贡献因素"，避免一本正经胡说八道 |
| r/sre 社区共识 | "野外 9/10 的 RCA 找的是触发器不是根因" | 人工复核环节不可省（我们的 HITL 文化天然契合） |

### 2.5 调研总表

```
成熟可抄：OTel servicegraph→Node Graph（RED 上图）、ArchUnit 一致性断言、
          C4/LikeC4 清单即事实源、DeepWiki 式 LLM 逆向
前车之鉴：CodeSee（纯可视化死）、UML round-trip（同步引擎死）、
          AI 硬猜根因（改说贡献因素）
空白机会：LLM Agent 的节点级拓扑 + 意图层标注 + 图上错误定位 三合一
```

---

## 3. 核心判断

1. **分块问题不存在——你们已经有分块**。不按行/函数分，按**架构单元**分：`路由 → 图节点 → 工具 → 外部依赖 → 定时任务`。映射表：

   | 架构单元 | 代码位置 | 已有清单 |
   |---|---|---|
   | API 路由 | app/main.py + app/api/compat.py | main.py 路由注册 |
   | 图节点 | app/graph/build.py（prep/agent/tools） | LangGraph 结构本身 |
   | 工具 | app/tools/*（9 个） | **TOOL_REGISTRY（现成！）** |
   | 外部依赖 | LLM 网关/智谱/Qdrant/embedding/MySQL/PG | settings.yaml endpoints（现成！） |
   | 定时任务 | daily_digest / rag_reconcile | _start_scheduler（现成！） |

2. **图 = f(绑定清单, 遥测)**，二者皆机器可读，图永远可重算——这就是"实时更新"的全部秘密，不需要任何同步协议。
3. **Ops Agent 是角色不是新系统**：复用现有 FastAPI + APScheduler + mail + ledger + LLM 池，新增一个 ops 域（权限矩阵里 `domain: ops` 早就预留了）。

---

## 4. 方案设计

### 4.1 绑定层 `config/modules.yaml`（新，唯一要写的新"事实"）

```yaml
modules:
  - id: api_compat            # 稳定 ID（图节点主键，遥测标签用它）
    name: 兼容 API
    layer: 接入
    code: [app/api/compat.py, app/main.py]
    owner: 开发者
    notes: 旧 Go 契约适配；非流式输出
  - id: node_agent
    name: agent 推理节点
    layer: 图执行
    code: [app/graph/build.py]
    deps: [llm_gateway, llm_zhipu]
  - id: tool_kb_search
    name: kb_search
    layer: 工具
    code: [app/tools/chat/kb_search.py]
    registry: kb_search      # ← 与 TOOL_REGISTRY 交叉校验
    deps: [qdrant, embedding]
  # ... 约 18 个模块；种子脚本可自动生成 80%
```

**一致性断言**（扩展 `tests/test_registry.py`）：registry 里每个工具必须在 modules.yaml 有对应模块——防漂移的 fitness function，CI 里跑。

### 4.2 遥测层（改 metrics.py，约 30 行）

- 现有 `/metrics` 加带标签计数器：`agent_tool_calls_total{module,status}`、`agent_module_latency{module}`（P95 由 Prometheus 算，或进程内环形窗口）。
- runs 表已有 `error` 字段——**错误签名映射表**（确定性初定位）：

```python
SIGNATURES = [
  ("AllEndpointsFailed",  ["llm_gateway", "model_pool"]),
  ("server_restart",      ["run_lifecycle", "deploy"]),   # 已证实：9/12 失败
  ("qdrant|timeout",      ["rag_store", "frp_tunnel"]),
  ("stale_cleanup",       ["run_lifecycle"]),
]
```

规则没命中的进 LLM 归因队列（给模块清单+错误样本+代码路径，让它输出嫌疑排序——"贡献因素"措辞）。

### 4.3 投影层：`GET /ops/topology`（admin）+ 单页可视化

- JSON 契约：`{nodes:[{id,layer,status,metrics:{rate,err,p95}}], edges:[{from,to,rate,err}], annotations:[...]}`——前端与后端的唯一接口，先做自建 SVG 页（预览图即雏形），远期可接 Grafana Node Graph。
- 渲染顺序：modules.yaml 定结构 → /metrics 定颜色/粗细 → annotations 表定卡片。**三个数据源各管一层，永远一致**。

### 4.4 意图通道（诉求 3 全链路）

```
用户："我希望搜索能支持拼音模糊匹配"
 → 小秋：submit_optimization(category, title, summary, original, suggested_modules?)
          （I8 submit_feedback 同款：LLM 整理+限流+台账）
 → 回复"收到，已转交运维通道"（异步，不阻塞对话）
 → Ops Agent job：LLM 将工单定位到模块（给 modules.yaml 上下文做匹配）
    ├─ annotations 表插入：{module_id, type: optimization, status: open}
    ├─ 图上该模块出现标注气泡（预览图右下卡片）
    └─ 邮件 devs 组（复用 mail 白名单，新增 devs 收件组）——主题带 [模块名]
 → 开发者处理完在图上/管理端关闭 → 状态 closed
```

### 4.5 Ops Agent 监控大脑（诉求 4）

- **触发**：APScheduler 每 10 分钟（与 rag_reconcile 同班）扫描窗口内 runs 失败聚类。
- **定位**：签名表（确定性）→ 命中即上图；未命中 → LLM 归因（输入：错误样本+模块清单+代码路径，输出：嫌疑模块排序+理由），**低于阈值只标注不上报**（防噪音）。
- **呈现**：图节点红环 + 事件卡片（含嫌疑代码位置、建议动作、关联 run 数）；同签名 10 分钟去重合并一封邮件（复用 mail 去重窗口）。
- **回看**：actions 台账记 `ops_agent/incident` 类型——审计与"你最近做了什么"的叙事闭环。

### 4.6 隐私与权限

- `/ops/topology` 与 ops agent 工具全部 `min_role: admin`（ops 域）；指标不含用户内容，只有聚合数。
- 意图工单内容进 PG 时沿用反馈管道的截断与脱敏策略。

---

## 5. 分期路线（每期不推翻前一期）

| 期 | 内容 | 工作量 | 验收 |
|---|---|---|---|
| **P0 绑定与投影** | modules.yaml + 种子生成脚本 + CI 一致性断言 + /ops/topology + 静态 SVG 图页 | 1-2 天 | 图上节点数==modules.yaml 条目数；改 registry 不改 yaml 时 CI 红 |
| **P1 遥测上图** | metrics 加 module 标签 + 错误签名表 + runs 聚类 job → 图上错误率/流量/红点 | 2-3 天 | 人工 kill 一个依赖 → 5 分钟内图上对应模块变红 |
| **P2 意图通道** | submit_optimization 工具 + annotations 表 + devs 邮件 + 图上标注 | 1 周 | 用户经小秋提优化 → 收到邮件 + 图上出现卡片 |
| **P3 Ops 大脑** | LLM 归因（贡献因素式）+ 事件卡片 + RCA 评测集（precision/recall） | 1-2 周 | 注入历史真实失败 → 初定位命中率的量化报告 |

---

## 6. 预览图说明（`ops_dashboard_preview.html`）

单文件、零依赖、暗色主题。内容基于**本项目真实架构与真实生产数据**（2026-09-10 runs 表：52 runs / 12 失败 / 9 个 server_restart / 3 个 stale_cleanup），叠加两类演示标注：

- **critical 红环**：run_lifecycle 节点（server_restart ×9，初定位=部署无 drain，嫌疑代码 main.py lifespan）
- **warning 黄环**：qdrant 节点（frp 链路抖动，来自记忆中的已知事实）
- **意图卡片**：compat SSE 节点挂着"用户优化请求 #17：流式无等待提示 → 已邮件开发者"
- **流量边**：宽度∝请求量、颜色=健康/异常、虚线动画=活跃流；LLM 网关为最热路径
- 顶部 stat tiles + 右侧事件流 + 图例；节点 hover 显示指标与责任代码路径

---

## 7. 参考来源

**图-代码耦合**：[LikeC4](https://likec4.dev/) · [LikeC4 GitHub](https://github.com/likec4/likec4) · [DeepWiki](https://deepwiki.com/) · [Devin 文档](https://docs.devin.ai/work-with-devin/deepwiki) · [deepwiki-rs](https://github.com/sopaco/deepwiki-rs) · [GitKraken 收购 CodeSee](https://gitkraken.com/blog/gitkraken-launches-devex-platform-acquires-codesee) · [CodeSee 复盘](https://www.loot-drop.io/startup/2500-codesee)

**双向同步史**：[Round-trip engineering (Wikipedia)](https://en.wikipedia.org/wiki/Round-trip_engineering) · [Hailpern & Tarr, IBM Sys. J. 2006](https://bitsavers.trailing-edge.com/pdf/ibm/IBM_Systems_Journal/453/hailpern.pdf) · [InfoQ: 8 Reasons MDE Fails](https://www.infoq.com/articles/8-reasons-why-MDE-fails/) · [ThoughtWorks Fitness Functions](https://www.thoughtworks.com/en-us/insights/articles/fitness-function-driven-development) · [Archyl 漂移检测指南](https://www.archyl.com/blog/architecture-drift-detection-guide)

**运行时拓扑**：[Grafana Tempo Service Graph](https://grafana.com/docs/grafana/latest/datasources/tempo/service-graph/) · [OTel servicegraph connector](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/connector/servicegraphconnector/README.md) · [Tempo 指标查询](https://grafana.com/docs/tempo/latest/metrics-from-traces/service_graphs/metrics-queries/) · [LangSmith Observability](https://www.langchain.com/langsmith/observability)

**AI 运维**：[Sentry Seer](https://docs.sentry.io/product/ai-in-sentry/seer/) · [Rootly](https://rootly.com/) · [incident.io](https://incident.io/)
