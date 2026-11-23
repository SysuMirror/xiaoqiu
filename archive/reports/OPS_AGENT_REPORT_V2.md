# 小秋 Ops Agent V2 · 后端全链路图-代码耦合 —— 需求梳理与调研报告

> 状态：v2 · 2026-09-12 · **取代 v1（OPS_AGENT_REPORT.md）**——v1 把视角放在了 agent 自身，v2 纠正：**主角永远是后端，小秋只是控制面**
> 调研：两轮共 15 项定向检索（官方一手源为主），叠加对本仓库 39k 行 Go 代码的实际勘察
> 产出：本报告 + 预览图 `ops_backend_chain_preview.html`

---

## 0. 一页结论

要造的东西一句话：**给 SSE Market 后端造一张"活的链路地图"——从代码生成（不是画的），叠加实时遥测（哪条链在跑、多快、错多少），叠加意图层（bug 定位、优化请求），再配一个能读懂这张图、能在图上干活、但动手前必须过人工审批的运维 Agent（小秋）。**

```
代码仓库 (routes.go/main.go/分层目录)          后端运行时 (/metrics RED)
        │ 生成器 gen（静态分析，可重算）              │ 30s 差分采集
        ▼                                            ▼
┌─────────────────── 绑定清单 backend_map.yaml（唯一事实源）───────────────────┐
│  接入 → 中间件管道 → 控制器(按业务域) → 服务层 → 仓储 → 存储/外部依赖          │
└──────┬──────────────────────────────────────────────────┬────────────────┘
       ▼ 投影(渲染永远可重算)                        ▼ 意图写入
  链路图(结构+行为)  ◀──错误定位/优化标注（卡片）──  小秋 Ops Agent + 开发者
```

**五条裁决**（§3，全部有调研支撑）：
1. 图是投影不是档案——生成而非维护，业界已成共识
2. 分块按"请求的旅程"分，中间层必须显式——这是你们点名要的
3. 静态分析打底、LLM 加语义、且 LLM 结论必须经静态分析校验（CodeBoarding 配方）
4. 错误定位 = 拓扑告警关联（沿链路向下游抑制、向上游找根因），规则先行 LLM 补充
5. 一致性靠生成器进 CI，不靠人的自觉

---

## 1. 需求梳理：两个视角

### 1.1 你们的五点诉求 → 能力映射

| # | 原话 | 能力 | 实现载体 |
|---|---|---|---|
| 1 | 代码分块，链路关系清晰，**不仅是接口还有中间层** | 按"请求的旅程"建模：接入→中间件管道→控制器→服务→仓储→存储/外部依赖 | 绑定清单 + 生成器 |
| 2 | 流程图与代码块绑定，AI 能理解对应关系，炫酷可视化 | 结构投影（图上每个节点可点击回代码文件/路由）；LLM 语义层给模块加"业务流"命名 | 生成器 + LLM 语义标注 |
| 3 | 用户→小秋"要做哪些优化"→收到→异步→ops agent→邮件开发者 | 意图通道：结构化工单→定位模块→图上标注+邮件 | submit_optimization（复用 I8 管道） |
| 4 | 监控到 bug 初步定位代码/模块并上图 | 错误签名规则→模块→**链路高亮**（不只是点红，是标出断裂的那一段链） | 遥测差分 + 签名表 + annotations |
| 5 | **小秋只是辅助；全链路可视化监控后端、依赖关系、按报错判断断链** | 拓扑告警关联：错误沿依赖边回溯上游根因、抑制下游连锁告警 | 图遍历（业界 RCA 标准模式） |

### 1.2 后端开发者画像（v2 新增的核心视角）

一个后端同学对这张图/这个 Agent 的四个真实诉求：

| 阶段 | 内心独白 | 图/Agent 要给的 |
|---|---|---|
| **第 1 天上手** | "39k 行 25 个控制器，请求从哪进来、经过谁、落到哪？我该从哪读起？" | 链路总图 + 每模块"读码入口"（文件+路由数）；CodeMap 论文实证：代码地图显著提升新人理解速度 |
| **改代码前** | "我改 user_service.go 会影响谁？" | **反向依赖查询**：模块→上游调用者（哪些控制器/路由依赖我）；这是图比 grep 强的地方 |
| **半夜被告警叫醒** | "500 了。是我这层还是下游？哪条链断了？" | 错误卡片直接给出**嫌疑链路段** + 上游回溯结论（"music 域错误，上游中间件正常，根因候选：api/cosBucket.go"） |
| **信任问题** | "图会不会骗我？"（过期的图比没有图更毒） | 图 100% 可重算（生成器+CI 防漂移）；遥测实时；图上每项数据带来源角标 |

### 1.3 领域模型

图 = 三层叠加，各层生命周期完全不同、互不污染：
- **结构层**（变化=随代码提交）：绑定清单 yaml，生成器从代码提取，CI 校验
- **行为层**（变化=随秒级遥测）：RED 指标挂在模块/边上（差分计算，永不漂移）
- **意图层**（变化=随人类活动）：bug 卡片、优化请求、运维动作记录（annotations 表）

Ops Agent（小秋）= 这张图的**值班员**：读图回答问题（L1）、写标注（L2）、在图上动手（L3·HITL 审批）。

---

## 2. 调研消化（两轮 15 项检索，2026-09-12）

### 2.1 图从哪来：**"生成而非维护"已成业界共识**

- [Multiplayer.app](https://www.multiplayer.app/blog/diagram-as-code-tools-are-just-a-step-towards-the-tools-we-need/)：Mermaid/PlantUML 这类 diagram-as-code 只是过渡态，终点是**与代码实时同步、永不漂移的图**；[madewithlove《架构图已死》](https://madewithlove.com/blog/the-architecture-diagram-is-dead/)：图会过期因为它是第二份要人维护的工件，解法是从代码/基础设施生成；Lobsters 共识："过期图是流程失败，图应该由流水线生成"。
- 工具实例：[Swark](https://github.com/swark-io/swark)（VS Code 内 LLM 从代码生成架构图）、Documint、[Catio](https://www.catio.tech/blog/architecture-diagrams)（模型驱动，服务/依赖变化自动反映）。
- **对我们的含义**：不引入这些工具本身，而是采纳其原则——我们的图每个像素都能从"代码+配置+遥测"重算出来。

### 2.2 AI 理解代码的正确配方：**静态分析打底 + LLM 加语义 + 用前者校验后者**

两个当红开源项目给出了完全一致的配方（这直接回答诉求 2 的"AI 能理解哪块代码对应哪个功能"）：
- **[Understand-Anything](https://github.com/Egonex-AI/Understand-Anything)**（71k stars）：5-agent 流水线，Tree-Sitter 静态分析抽取文件/函数/依赖（确定性）→ LLM 语义层把代码分组为"认证流/支付流"等业务概念 → 知识图谱 JSON → 可交互地图 + 问答。
- **[CodeBoarding](https://github.com/Codeboarding/CodeBoarding)**：以**控制流图为 ground truth**，LLM 分析结论**必须经静态分析校验以抑制幻觉**，产出多级可缩放架构图。
- 配方要义：LLM 永远不做结构的唯一来源——结构来自确定性解析（我们=routes.go/main.go/imports），LLM 只负责"起好名字、归对业务域、写摘要"，且可被校验。
- 开发者上手侧的实证：[CodeMap 论文（arXiv）](https://arxiv.org/html/2504.04553v3)人类实验证实 AI 代码地图显著提升理解直觉与有用度感知；[DeepWiki](https://deepwiki.com/)（自动 wiki+架构图+问答）与 [Sourcegraph Cody](https://sourcegraph.com/docs/cody)（企业级代码问答）是该赛道成熟形态。

### 2.3 错误怎么定位：**拓扑告警关联是业界 RCA 的骨干模式**（正中诉求 5）

- [middleware.io](https://middleware.io/blog/alert-correlation-strategies-distributed-microservices/)：**沿依赖图遍历**自动找真正的上游根因；[OneUptime（2026-01）](https://oneuptime.com/blog/post/2026-01-30-alert-correlation/view)：拓扑关联用依赖图定位根因并**抑制下游连锁告警**（一条链断，只有根因点告警，不刷屏）；[Dynatrace Davis](https://docs.dynatrace.com/docs/dynatrace-intelligence/root-cause-analysis/concepts)：在实时拓扑图（Smartscape）上做确定性因果分析；2026 学术界（ScienceDirect）：图增强多智能体 RCA 是当前前沿方向（[综述 arXiv](https://arxiv.org/html/2408.00803v1)）。
- **对我们的含义**：这就是"快速根据报错判断后端出现错误的链路"的成熟答案——错误挂到模块后，沿绑定清单的边做上游回溯 + 下游抑制，输出"嫌疑链路段"。规则（签名表）先做确定性定位，LLM 归因只在规则未命中时补充（措辞用"贡献因素"，Rootly 的教训）。

### 2.4 Go 生态的静态分析底料

[godepgraph](https://github.com/kisielk/godepgraph) / [Go Architect](https://go-architect.github.io/docs/analysis-tools/dependency-graph/)（包依赖图）、go-arch-lint（架构规则 lint）、golang.org/x/tools（调用图）。**对我们的含义**：Go 的 import 系统天然给"分层归属"提供确定性数据（controller 只能 import service，违规即架构告警）——远比 Python 好绑定，是我们的运气。

### 2.5 历史教训（v1 报告已载，仍然有效）

- **UML round-trip/MDD 坟场**（[IBM Hailpern&Tarr 2006](https://bitsavers.trailing-edge.com/pdf/ibm/IBM_Systems_Journal/453/hailpern.pdf)、[InfoQ](https://www.infoq.com/articles/8-reasons-why-MDE-fails/)）：双向同步引擎 30 年没人做成——"代码逆向到设计"的单向投影 + Agent 做桥才是活路。
- **CodeSee 之死**（[$10M 融资→2024 关停](https://www.loot-drop.io/startup/2500-codesee)）：纯可视化是维生素不是止痛药——图必须绑痛点（事故定位/影响分析），炫酷是结果不是目的。
- **HITL 共识**（[Kagent](https://kagent.dev/blog/human-in-the-loop-kagent)）：破坏性动作前 agent 必须暂停等人批。

---

## 3. 核心裁决（V2）

1. **图 = f(代码, 配置, 遥测, 意图)，四输入之外图上没有任何"手画"的东西。** 一致性问题从根上消解：结构漂移被 CI 拦截，遥测永不漂移，意图有版本化表。你们问"图和代码怎么保持一致"——答案是**图根本不维护**。
2. **分块 = 请求的旅程，六段式**（比 v1 的"控制器列表"更深一层，回应"不仅是接口还有中间层"）：
   `客户端 → nginx → Gin 中间件管道(每节一个子节点: RequestID→Tracing→Metrics→Logger→CORS→Auth三分支→BloomFilter) → 控制器(按业务域分组) → service 层 → repository 层 → MySQL/COS/缓存/外部(webhook)`
   中间件管道是真实存在的处理层（每条请求都过），也恰是限流/鉴权类故障的常发地——必须显式建模。
3. **LLM 的位置**：结构=静态分析说了算；LLM 给模块起业务名、归域、写"这模块干什么"的摘要、做错误归因——所有 LLM 产出带"AI 标注"角标且可被重新校验。
4. **错误定位流水线**：遥测差分→错误签名表（确定性，先做）→ 挂到模块 → **沿边上游回溯+下游抑制 → 输出嫌疑链路段** → 规则未命中才进 LLM 归因队列 → 图上红链+卡片+邮件（同签名 10min 去重）。
5. **反向依赖查询是给开发者的杀手锏**（grep 给不了的）：图建成后"改 X 影响谁"是一次边遍历——这决定了绑定清单必须存 `imports` 边，不只是调用链。

---

## 4. 方案设计（对准 SSE_market_server 真实结构）

### 4.1 绑定清单 schema（backend_map.yaml v2）

```yaml
pipeline:            # 中间件管道（顺序敏感，从 main.go + routes.go 提取）
  - {id: mw_request_id, name: RequestID,  code: [middleware/request_id.go]}
  - {id: mw_metrics,   name: Metrics,     code: [middleware/metrics.go], produces: "/metrics"}
  - {id: mw_auth,      name: Auth(Strict/Optional/OAuth), code: [middleware/authMiddleware.go, ...]}
  - {id: mw_bloom,     name: BloomFilter, code: [...], note: 仅 auth 组}
services:             # 控制器模块（18 个，routes.go 解析）
  - id: go_music
    domain: 音乐
    code: [controller/musicController.go]
    routes: ["POST /api/music/upload", ...]
    imports: [api, service]          # ← 静态提取，反向依赖查询的数据底料
    summary: "音乐上传/流播放；本地盘+腾讯云COS双写"   # ← LLM 生成，带角标
layers:               # service/repository 是真实目录分层
  - {id: svc_layer, code: [service/]}
  - {id: repo_layer, code: [repository/]}
deps:                 # 数据与外部
  - {id: mysql, kind: store, code: [common/database.go]}
  - {id: cos, kind: external, code: [api/cosBucket.go]}
  - {id: agent_hook, kind: external, code: [controller/raghook.go], to: sse-agent}
edges: [...]          # pipeline→service→layer→dep 全量边（生成）
```

### 4.2 生成器 gen v2（升级现有 gen_backend_map.py）

- routes.go → 控制器+路由（已有）；**新增**：main.go 中间件注册顺序解析、go imports 静态提取（`golang.org/x/tools/go/packages` 或正则起步）、分层目录映射
- `--check` 进 CI：路由/中间件/import 边任何漂移即红
- LLM 语义作业（一次性+按需）：模块摘要、业务域命名校准——产出物写回 yaml 的 AI 字段，永不覆盖确定性字段

### 4.3 遥测与错误定位

- 已有 Prometheus per-route histogram（middleware/metrics.go）+ 本地已建差分采集（go_route_stats）
- 错误签名表 v2（含链路段）：

```python
SIGNATURES = [
  ("cos.*timeout|cos.*5xx",   chain=["go_music","cos"],       root="cos"),
  ("429",                     chain=["mw_auth","login_rate"], root="mw_auth"),
  ("sql.*timeout|Error 1064", chain=["go_*","repo_layer","mysql"], root="mysql"),
  ...
]
```

- 定位输出三件套：**红链高亮（哪一段）+ 根因候选（哪个节点/文件）+ 抑制声明（哪些下游告警是连锁，不重复报）**

### 4.4 意图通道（诉求 3，全链路）

用户→小秋："希望帖子搜索支持拼音" → `submit_optimization`（LLM 整理+限流，I8 同款）→ Ops Agent 拿绑定清单上下文定位模块（go_post）→ annotations（kind=optimization）+ devs 邮件 → 开发者在图上看到卡片 → 处理完关闭，图上消失。台账全程留痕。

### 4.5 开发者模式

- **上手路径**：图页"新人视角"开关——按"请求的旅程"顺序高亮主干 + 每节点读码入口
- **影响分析**：搜索框输入文件名/模块名 → 上游依赖者高亮（"改 user_service 影响：用户域 4 控制器 31 条路由"）
- **信任角标**：每个数据点 hover 显示来源（生成器版本/遥测窗口/AI 标注）

---

## 5. 分期路线

| 期 | 内容 | 验收 |
|---|---|---|
| **P0 结构层** | gen v2（中间件管道+imports 边+LLM 摘要）+ 链路总图页（六段式泳道）+ CI --check | 图上节点数==清单条目数；删掉一个中间件 CI 变红 |
| **P1 行为层** | 遥测差分上图（边宽=速率、节点色=错误率）+ 热点路由表 | kill 后端容器→图上对应段 5 分钟内变红 |
| **P2 定位层** | 错误签名表 + 链路回溯/下游抑制 + 红链卡片 + devs 邮件 | 注入 COS 超时→卡片给出正确嫌疑链+根因候选 |
| **P3 意图+上手** | submit_optimization 闭环 + 新人视角 + 反向依赖查询 | 用户提优化→邮件+图上卡片；输入文件名→影响面正确 |

## 6. 预览图说明

`ops_backend_chain_preview.html`（单文件/零依赖/暗色）：**一条请求的旅程**横向泳道（客户端→nginx→中间件管道→业务域控制器→服务/仓储→数据与外部），叠加三样演示数据——① COS 超时错误的红链定位（含上游回溯结论与下游抑制声明）② 帖子模块的优化请求标注 ③ 登录热点流量边动画；右侧开发者栏含"反向依赖查询"与"新人 5 分钟上手路径"；底部一致性条说明图的生命周期。悬停任何模块可看代码路径/路由数/指标来源。
