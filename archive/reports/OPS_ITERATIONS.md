# Ops Agent 多角色迭代循环记录（五轮 · 2026-09-12）

> 方法：开发者锐评 → Agent 质询 → 开发者补充 → Agent 记录 → 运维锐评 → Agent 综合设计 → 界面/需求重构判断 → 方案 → 代码。
> 产出：backend_map v3（六段建模）+ 值守 watchdog + 聚焦模式观测台。验收 tests/i10_ops_iteration.py 全 PASS。

---

## 第 1 轮 · 缓存架构不可见（用户点名的例子，实测更严重）

- **开发者**：图上 `cache·mq` 是"待登记"灰盒——实际上缓存是最重要的架构决策：L1 内存逻辑过期 + L2 Redis 防 MySQL 击穿（public_feed/home_browse 靠它扛量），6 个 Profile 各有策略。新人看图会以为每次请求都打 MySQL，做出错误判断。Redis 是硬依赖却不存在。
- **Agent 质询**：建模到什么粒度——路由级还是 Profile 级？失效链路画不画？
- **开发者补充**：Profile 级足够（每个挂"保护域"）；Redis 必须是一级依赖节点带健康位。
- **运维锐评**：必须能表达"Redis 挂了退化到哪"——回源风暴场景；缓存命中率将来上图，先留位。
- **Agent 记录/设计**：cache/config.go 的 Profile 常量进生成器（确定性解析 TTL/逻辑过期）；redis 入 infra（双角色：L2+延迟队列）；cache_layer 作为分层节点。
- **重构判断**：界面局部改；需求扩容（"数据/外部"升维为"数据/缓存/异步"）。
- **踩坑**：Profile 字面量块结尾是 `\n\t}`（带 tab），`find("\n}")` 失配导致块越界、标志位被后续块污染——改用"第一个 }"切块并写对齐断言。

## 第 2 轮 · 异步链路盲区

- **开发者**：mq 是 Redis 延迟队列（ZSet+Hash，1h 延迟，死信重试），消费端是**另一个仓库的 Python 服务**（server-task）——跨服务链路图上不存在；heat/lottery/pin 三个常驻 goroutine 同样隐形。排查"帖子为何 1h 未归档"无处下手。
- **Agent 质询**：跨仓库消费端怎么绑定？→ **开发者**：按外部依赖登记（同 raghook），code 指向对面仓库。
- **运维锐评**：异步任务的故障模式是"静默不跑"（goroutine panic 无人知）——图上必须有"最后心跳"位；接入前诚实标"未接入"。
- **代码**：main.go `go pkg.Fn()` 解析 → jobs 段（含 heartbeat 字段）；server_task 入 infra。

## 第 3 轮 · 排障下钻速度（目录 → 作战图）

- **开发者**：图是"看架构"的，半夜排障要的是：报错进来 → 嫌疑链路直接放大、其余变暗、附定位依据。
- **Agent 质询**：签名库放哪、谁维护？→ **开发者**：agent 侧 python，chain 引用模块 id。
- **运维锐评**：定位必须附"排除了什么"（上游正常/下游属连锁）——没有排除声明的定位是耍赖。
- **代码**：app/ops/signatures.py（5 条文本签名：COS/Redis/MySQL/登录限流/MQ 积压，各带 chain+root+suppress+advice）；topology 给签名命中的标注附链路；视图 focusChain（点击卡片 → 链路成员高亮、其余 dim）。**已实测**：go_music+cos 高亮、17 元素变暗、排除声明显示。

## 第 4 轮 · 遥测盲区与自动值守

- **运维锐评**：签名表躺着没人查——要 watchdog 周期扫差分错误自动标注上图；依赖健康要主动探测（backend /ready、MySQL SELECT 1），不能等用户发现。
- **开发者**：规则命中不轰炸（同签名去重已有）；探不到的诚实标 unknown，不伪造健康。
- **代码**：app/ops/watchdog.py（scan：模块错误率阈值判定→自动标注含链路提示；probe_deps：trust_env=False 防代理坑）；调度 5min；E2E 测试走通"注入差分→自动标注→去重→清理"。
- **运维实战收获**：①探测必须 `trust_env=False`（Windows 系统代理会劫持内网探测）；②采集/探测地址必须 `127.0.0.1`（云服务器访问自身公网 IP 是 NAT 回环超时——高频踩坑榜第一的变体，已在 settings.yaml 修正）。

## 第 5 轮 · 数据资产与一致性收口

- **开发者**：图只画请求流，没画"数据在哪"——音乐本地盘+COS 双写、MySQL 存什么、agent PG 存什么，新人要翻文档。
- **运维锐评**：资产视图也是排障与恢复依据（"数据从哪恢复"）。
- **代码**：backend_map v3 增 data_assets（5 项：MySQL/本地盘/COS/agent PG/Redis-可重建标注，来源真实文档）；视图数据资产面板；本文件作为循环留档。

---

## 总裁决记录（跨五轮）

| 判断项 | 结论 |
|---|---|
| 界面是否重构 | 第 3 轮结构性增强（泳道化+聚焦模式），其余局部迭代 |
| 需求是否重构 | 第 1 轮实质扩容（缓存/异步域入模），其余确认原方向 |
| 仍开放 | 缓存命中率上图（需后端补 counter）；jobs 心跳接入（需后端打点）；LLM 归因（P3） |

---

# 第二轮循环 · 十轮（画布重造 · 2026-09-12）

> 起因判词：**"流程图是个摆设"**——上一版图只是页首装饰带，真正干活的还是表格；那不是流程图，是带箭头的思维导图。本轮十循环把"图即应用"立为主线，聚焦三命题：该有什么功能 / 流程图怎么表现 / 模块与代码关系怎么表示。

| 轮 | 开发者批评 | 运维补充 | 落地 |
|---|---|---|---|
| 1 | 图是摆设：主交互还是表格，图只是装饰 | 画布必须能平移缩放，图是主界面 | **整体重造为 SVG 画布**（滚轮缩放/拖拽平移/fit），表格退居侧栏 |
| 2 | "列"不是边——真实调用关系（svc→repo→mysql、cache 回源、mq→redis→消费端）必须在图上画出来 | 边要分同步/异步（虚线）、可 hover 看语义 | 真实边渲染 + 语义标签 + 同步实线/异步虚线 |
| 3 | 模块和代码的关系点不进去 | 要能一路钻到"这条路由是哪个函数处理的" | **三级钻取**：域分组→模块→路由列表→handler 函数（gen 新增 routes_detail，--check 同步校验）；控制器↔service 文件命名约定反推 |
| 4 | 排障时图不动手——告警卡和图是两张皮 | 错误链路是图上发光路径，其余变暗 | 聚焦模式 v2：点告警→链路红边动画+节点高亮+其余 dim；**无常规边时补画错误直达边**（业务→依赖走分层的缺口） |
| 5 | 图是死的，看不到"请求在流" | 热点要看得见 | **粒子流**：边上粒子动画（热边双粒），错误粒子红色；演示流量开关（无真实遥测时明标"模拟"） |
| 6 | 画布不能只是看，要能整理 | 手工布局要记住 | 节点拖拽 + localStorage 布局记忆 + **搜索定位**（Ctrl+K 搜模块/路由/文件名→跳转+闪烁高亮） |
| 7 | 中间层要么是关卡要么别占地方；业务域要能当组看 | WS/异步边样式要区分 | 中间件渲染为主干道上的**关卡胶囊**；业务域分组容器；WS 模块标记 |
| 8 | 请求流之外还有数据流——"数据在哪"图上没有 | 恢复视角：写路径+备份 | **数据视图开关**：资产节点 + 写路径边（repo→MySQL、music→COS/本地盘、mq→redis）+ 资产卡（备份方式） |
| 9 | 一堆节点会迷路 | 图例/缩放控件/提示不可少 | 图例面板、缩放按钮、底部操作提示、Escape 清聚焦、Tab 可聚焦 |
| 10 | 收口：功能多了会不会互相打架 | 全链路回归 | i10 回归通过 + 浏览器全交互链实测（选节点→handler 钻取/搜索命中/聚焦 2 亮 33 暗 1 红边/演示开关/数据视图） |

## 十轮总裁决
- **界面**：第 1 轮即判"完全重构"——dashboard 形态废弃，canvas 形态确立（单文件 ~750 行，零依赖）
- **需求**：新增两条正式需求——"路由级→handler 级绑定"（第 3 轮）、"数据视图"（第 8 轮）；其余为表现层确认
- **经验**：Browser pane 最小化时 requestAnimationFrame 冻结（粒子 0 帧是环境特性非 bug）；聚焦链路若无现成边必须补画直达边

---

# 第三轮循环 · 十轮（Bug 修复 + AI 友好层 · 2026-09-12）

> 判词：画布大升级但 bug 多——节点全成小白球、无类型区分、初始堆叠。修完后的主线：**图不仅方便人，还要方便 AI**。

| 轮 | 开发者/运维意见 | 落地 |
|---|---|---|
| 1 | 白球+文字不清=自定义节点没生效 | 二分定位（单节点→边→全节点→按类→逐节点）：**业务 type 侵占了 G6 渲染 type 字段**（'svc' 未注册→回退圆形）；改名 kind。另两个连带 bug：client 节点漏传 name（draw 炸）；lineDash:null 令 @antv/g 崩 |
| 2 | 要类型区分、间距自然 | 九类型=形状+左色条+几何图标（蓝网关/盾牌中间件/格子业务/绿分层/圆柱数据库/云外部/时钟任务/机器人控制面，path 手绘零依赖）；dagre nodesep 34/ranksep 96 + combo padding 加大 |
| 3 | "改这里影响谁"要能带出去 | 节点详情加**复制模块卡 JSON / 复制 ID**（粘给 IDE/AI 即可定位） |
| 4 | 排障要机器人代劳 | **/ops/triage**：确定性四问（什么坏了/影响谁/哪条链/排除什么）+ findings + 签名建议；侧栏"运维机器人"面板一键诊断 |
| 5 | md 记录架构、强绑定图 | **docs/BACKEND_MAP.md 由 gen 代码生成**（模块表含代表性路由→handler、管道、缓存、资产）；--check 校验 md 漂移（手改即 CI 红） |
| 6 | 模块描述落库、更新时重生成 | **ops_module_cards 表 + gen --sync-db upsert**（18 张卡；幂等） |
| 7 | AI 要有全局图 | /ops/context?module= 单模块上下文卡（repo-map 思路：routes+handlers+service_impl+链路提示+缓存策略+健康+ai_note）；/ops/map.md 机读文档 |
| 8 | 调研不造轮子 | G6 类型区分/间距/图标确认用官方扩展点（registerNode path 图标）；context 卡对标 Aider repo-map/DeepWiki 结构化上下文实践 |
| 9 | 界面/需求重构判断 | 界面：R1-R2 修复性重构（渲染层）；需求：正式新增"AI 友好层"（context/triage/md/cards 四件套为一等公民） |
| 10 | 收口回归 | I9/I10/I11 三套全 PASS + 浏览器实测（九类节点/复制三钮/机器人报告 5 卡/聚焦/搜索） |

**沉淀**：/ops/context（AI 上下文卡）· /ops/triage（四问排障）· /ops/map.md（机读架构文档）· ops_module_cards（落库模块卡，--sync-db 幂等）· 前端复制出口+机器人面板。

---

# 第四轮循环 · 十轮（测试循环：开发者模拟使用 → 报 bug → 修复 · 2026-09-12）

> 方法：以开发者身份真实操作（浏览器 DOM 驱动 + 端点压测），每轮"使用→发现→修复→复测"。

| 轮 | 使用场景 | 发现的 Bug | 修复 |
|---|---|---|---|
| 1 | 打开页面看指标 | music 节点永远显示 34.8% 假错误率——**陈旧快照被当实时指标**（几小时前的注入帧照样算错误率） | aggregate 只取窗口 3 倍时长内的新鲜快照，过期视为无遥测 |
| 2 | 逐类节点点详情 | job/控制面/网关节点缺"复制/诊断"三钮（前端白名单漏放） | 白名单补 job/ctrl/gateway |
| 3 | 后端边界 | /ops/context?module=ops_agent 404（非业务分支漏 control） | module_card 补 control 分支 |
| 4 | 搜索全场景 | 纯空格输入弹出 10 条命中（中文名含空格） | 输入 trim |
| 5 | 聚焦/退出 | （通过：2 亮 1 红链 1 补画边，Esc 全恢复，hot 样式保留） | — |
| 6 | 开关组合轰炸 | **演示流量切换后节点指标不更新**（G6 changeData 对已存在节点只更新 model 不重画自定义 shape）；数据视图下搜索命中不可见节点点击无反馈 | rebuildGraph（销毁重建）+ 跳转失败 toast |
| 7 | 窗口尺寸变化 | **隐藏容器建图尺寸为 0；resize 后画布不重排**（G6 不自动 changeSize） | resize/visibilitychange/IntersectionObserver 三钩子 changeSize |
| 8 | 复制功能 | **页面未聚焦时 clipboard API 抛 NotAllowedError 且 copyCard 无 try/catch 整链炸死** | 兜底链：clipboard→execCommand→明确指引 toast |
| 9 | 性能压测 | **所有端点 3s+**（每次请求同步外呼探测不可达后端，无缓存） | 探测 60s TTL 缓存 + build 只读缓存 miss 后台刷新（补了后台任务不写回缓存的连带 bug）+ 超时 5s→2s |
| 10 | 收口回归 | i10 断言 deps_health 缺 go_server（首次为空 dict 破坏契约） | miss 时返回 unknown 四键骨架（"探测中"） |

**成效**：端点延迟 3s+→0.1-0.3s；假错误率消失；全类型节点可用复制/诊断；三套验收（i9/i10/i11）全 PASS。
**方法论沉淀**：G6 三坑入册（type 字段侵占/changeData 不重绘/需手动 changeSize）；clipboard 必须兜底；外呼探测必须 TTL 缓存且不得阻塞主请求路径。

---

# 第五轮循环 · 十轮（产品/开发/运维三角 → LangGraph Ops Agent · 2026-09-13）

> 判词：AI 助手已是真实 LLM，但仍是"单次调用"——不能看图、不能查事实、无记忆、无审计。
> 用户指令：可以考虑 langgraph 之类的架构了。

| 轮 | 视角 | 痛点/需求 | 落地 |
|---|---|---|---|
| 1 | 产品 | AI 说的模块 id 用户要自己找 | **modref**：回复中模块 id 渲染为可点标签，点击=选中+图上跳转 |
| 2 | 产品 | 每次干等 5-15 秒 | **SSE 流式**（/ops/chat/stream：token 逐字 + tool 事件） |
| 3 | 开发 | LLM 只见系统提示摘要，深入问题答不了 | **LangGraph ReAct Agent**：五只读工具（get_module_card/list_modules/search_routes/triage_module/list_annotations），LLM 自主调用取事实 |
| 4 | 开发 | 对话无记忆 | history 前端维护（最近 8 轮传后端，无状态全局开放下不落会话库） |
| 5 | 运维 | AI 调用无审计 | 每轮落 actions 台账（q 摘要/tools 次数/model） |
| 6 | 运维 | 全局开放端点可被刷钱 | IP 限流 12/min（进程内滑窗；瞬时连打实测被拦） |
| 7 | 产品 | 告警出现时不知能问 AI | 事件流行加"问AI"（预填诊断问题） |
| 8 | 开发 | triage 与 chat 两套割裂 | triage_module 工具化，Agent 排障先取确定性结论再推理 |
| 9 | 运维 | 模型降级状态不可见 | AI 页头部 modelBadge + 气泡页脚（model·Agent·工具次数） |
| 10 | 收口 | 全链路验收 | i13 T1-T4 全 PASS + i9-i12 回归全 PASS + 浏览器实测（流式渲染/modref 可点/徽章） |

**三个 LangGraph 深坑（本轮最大收获，全部实测复现后修复）**：
1. **StateGraph(dict) 的 messages 是覆盖语义**——必须 `Annotated[list, add_messages]`（TypedDict 状态），否则 AIMessage(tool_calls) 被 ToolMessage 覆盖 → 智谱 1214"messages 参数非法"
2. **模型回落必须在运行时**：1113（无资源包）在 ainvoke 时才暴露，建图期预判不到
3. **astream_events 只在模型真流式时有 token 事件**：节点内用 ainvoke 一个 token 都吐不出，须 astream 聚合 chunk

---

# 第六轮循环 · 生产级化（功能高可用 · 2026-09-13）

> 判词：Agent 功能齐了，但离"生产级"差三道防线（循环无上限/并发无节制/降级梯不一致），另有三处冗余代码链路；AI 头像待接线。
> 用户指令：向"功能高可用/生产级 Ops Agent"迭代，不造轮子参考业界成熟实践，**及时清理冗余链路和冗余代码**。

| 轮 | 视角 | 痛点/风险 | 落地 |
|---|---|---|---|
| 1 | 产品 | chatBubble 的 bot 头像还是文字"秋" | 真头像接线：原图 1254²/2.45MB → **128²/35KB**（22px 显示 3x 余量）；新 `/ops/static/{name}` 单目录白名单路由（resolve+parent 校验，穿越实测 404）；CSS 圆形裁剪（50%/cover + 绿环） |
| 2 | 开发 | LangGraph 默认 recursion_limit=25 **不声不响**，死循环 Agent 烧钱无上限 | 显式 `RECURSION_LIMIT=24`（≈12 轮工具调用），ainvoke 与 astream_events 双入口下发；i16 T4 用 FakeGraph 断言 config 真收到 |
| 3 | 运维 | 全局开放端点并发无节制——一次 agent 运行 = N 次串行 LLM 调用，费用可被打爆 | `asyncio.Semaphore(4)` **bulkhead 凹槽**：槽满等 30s 超时→走降级梯而非拒绝；`ops_agent_inflight` gauge 实时上图 |
| 4 | 运维 | AI 调用次数/错误/延迟在 /metrics 完全不可见 | `ops_chat_requests_total{endpoint,status}`（sync/stream × agent/single/error/rate_limited/invalid）+ `ops_chat_seconds_total` 累计耗时 |
| 5 | 开发 | **降级梯两端点不一致**：流式 agent 全败直接 done{error}，前端还得二次非流式往返 | `stream_agent()` 把单次调用兜底搬进服务端：Agent 模型链 → 单次 ai.chat（done.degraded=single）→ 前端规则引擎，与 /ops/chat 同梯 |
| 6 | 开发 | 半途失败的流若降级，两段文本会拼进同一气泡 | **降级点必须在无副作用边界**：已吐过 token 的流只报"流中断"，绝不降级重答（i16 T4 场景 3 断言） |
| 7 | 维护 | /ops/chat/stream 内联 30 行生成器，与 run_agent 逻辑漂移过一次 | 生成器抽成 `agent.stream_agent()` 纯事件源（token/tool/done），端点只剩 SSE 包装——降级梯从此可单测 |
| 8 | 维护 | 冗余链路三处（用户点名清理） | ①watchdog._Time_monotonic 蠢包装（同文件三种 time import 别名）删净；②两端点重复的 messages 组装 → `agent.assemble_messages()` 共用；③`agent.build_ops_graph` 旧签名（全仓 grep 零引用）删 |
| 9 | 收口 | 验收 | **i16 五项**全 PASS（静态资产/真实指标上图/凹槽单测/FakeGraph 降级梯三场景/组装单测）；i9-i13 全回归 PASS |
| 10 | 收口 | 铁律执行 | 浏览器 DOM 实测：真实对话全链路（头像 50%/cover/22px/空文字、41 节点画布、chatBubble/mdLite/aiSend/selNode 函数齐全——I14 吞函数教训不重犯）+ 截图留证 |

**坑与沉淀**：
1. **asyncio.Semaphore 跨事件循环绑定**：_LoopBoundMixin 把信号量绑到首个使用它的 loop——单测多次 asyncio.run 会炸 "bound to a different event loop"；每循环重建（服务端单 loop 无此问题）
2. **降级梯设计原则：降级点必须在无副作用边界**。已产生用户可见输出（吐 token）的流不能降级重答——拼接比失败更糟；宁可报错让前端整轮重来
3. ruff UP041 在 py311 目标下把 `except asyncio.TimeoutError` 改写为 `except TimeoutError`——3.11+ 两者同一异常，项目 requires-python>=3.11 所以此改写安全
4. 测试必须统一走 `.venv` python：miniconda 缺 asyncpg、且 i10/i11 的子进程路径处理在裸 miniconda 下 import 不到 app

---

# 第七轮循环 · 真实流量与主机守卫（生产监控 · 2026-09-13）

> 判词：观测台一直"无遥测"诚实降级——不是没数据，是没接线；服务器监控只有每分钟的哑日志，无判定无处置。
> 用户指令：捕捉真实流量与错误信息；监控整个腾讯云生产服务器；严重问题（如内存爆满）立刻阻断进程→快析→邮件报告（2215020897@qq.com）；评估自做 vs 沿用，不过度耦合。

## 服务器勘察结论（自做/沿用裁决）

| 已有 | 现状 | 裁决 |
|---|---|---|
| Go /metrics | **在线且开放**（容器 172.18.0.6:8080，含 43 条请求 RED 直方图 + cache_hit_ratio/browse_sync_lag 业务指标 + 真实 500）——之前采不到是 agent 配置指向 8000（那是别人的 gohttpserver 静态服务，乌龙） | **沿用**：修正配置即通，管道（采集/差分/聚合/上图）早就在 |
| 腾讯云监控 | barad/sgagent/云镜在跑，数据推云控制台，本机取数需云 API SecretId/Key+SDK | **不接云 API**（凭据+SDK 耦合）。小秋直读 /proc/meminfo、/proc/loadavg（容器内=宿主机真值）+ docker stats（sock 已挂）——零依赖 |
| memwatch.sh | crontab 每分钟追加 top10 到 11MB 无轮转日志，无阈值无告警 | **升级替换**为 host_memguard.py（轮转+判定+阻断+上报） |
| Go 邮件 | SendPlainEmail 现成（注释即"系统报警用"），SMTP 凭据在 Go 侧 | **沿用**：Go 新增 /internal/mail（X-Internal-Token+收件人白名单+1封/分钟限频），凭据单一来源不复制 |
| 进程阻断 | sse_agent 容器 PidMode 私有——看得见宿主内存、杀不了宿主进程 | **host 侧本地决断**（OOM 时刻控制面自身难保，"立刻阻断"不能依赖小秋） |

## 落地（四层松耦合）

| 层 | 内容 |
|---|---|
| 采集 | Go /metrics（沿用，配置修正）+ app/ops/hostmetrics.py（/proc+docker.sock，Windows/无sock→None 诚实降级，gauge 上 /metrics，30s TTL 缓存）+ host 脚本进程级归因 |
| 守望 | scripts/host_memguard.py：纯 python3 自包含——可用<8% 且元凶>30% 且连续3周期 且非白名单 且出冷却 → SIGTERM(30s宽限)→SIGKILL；白名单绝不杀（sshd/docker/mysqld/nginx/小秋/Go后端/腾讯agent）；DRYRUN 干跑模式；--selftest 决策函数 8 场景自测 |
| 上报 | host → 小秋 /internal/ops/incident（X-Internal-Token，raghook 反向同款；结构预检 422；BackgroundTasks 立即响应） |
| 处置 | app/ops/guard.py：台账（永不丢）→ PROCESS_MAP 归因（./serve→go_server 等）→ LLM 快析（复用 ai.chat 回落链；失败→模板）→ Go /internal/mail 发 2215020897@qq.com → 观测台标注（同签名去重）；ops_incidents_total/guard_mail_total 上图 |

本地开发真数据：dev_tunnel.sh 动态解析 Go 容器 IP 建 8101 隧道 + .env GO_METRICS_URL 覆盖——本地观测台直接看生产真实流量（6/18→6/19 模块有遥测，go_vote 1.37 rps 领跑）。

## 坑与沉淀
1. **端口乌龙**：8000 是宿主机上无关的 gohttpserver（自带 401），不是 Go 后端——"Unauthorized"≠"metrics_guard 已部署"。勘察要 ss -tlnp 认 pid，别认端口号
2. **真数据接入打破测试封闭假设**：i10 watchdog 注入帧曾是"最近两个快照"，真实采集每 30s 落帧后注入帧被挤出差分窗口——环境相关测试必须清场（NOT LIKE 注入路由 DELETE）再注入
3. **gen --check 立功**：Go 新增 /internal/mail 立刻 DRIFT 报警——图-代码一致性守卫在 CI 语义下工作正常，重生成即愈
4. **ruff 扫出存量真 bug**：compat.py 三条收尾路径 metrics 用而未导入，NameError 被 finally 吞——用户无感但 agent_runs_finished_total 在 compat 路径从未上图（已补模块级导入）
5. /proc/meminfo、/proc/loadavg 不按 cgroup 隔离：容器内读=宿主机真值，这是主机监控的零耦合数据源；但 PID 是隔离的——进程归因与 kill 必须留在 host 侧
6. 后台化处置（BackgroundTasks）让上报响应毫秒级返回，host 脚本 3s 超时足够——处置慢（LLM 45s）不阻塞采集方

## 验收与部署
- tests/i17_ops_hostguard.py 五项全 PASS；i9-i16 全回归 PASS；浏览器 DOM 实测（42 节点、6 模块真实流量、host chip 降级隐藏、JS 函数齐全）
- **全部改动只在本地两仓库，线上未动**——部署清单见 DEPLOY_HOSTGUARD.md（nginx 本机-only 路由 /metrics 与 /internal/、Go 镜像重建带 INTERNAL_TOKEN+INTERNAL_MAIL_TO、agent .env 接线、host 脚本替换 crontab）

---

# 第七轮循环 · I18 生产观测补齐（磁盘 / 业务指标 / 错误率告警邮件 / 面板入口 · 2026-09-14）

> 判词：I17 上线后发现三个盲区——内存有了磁盘没有（盘已 82.4% 才知道该看）、/metrics 里业务 gauge 躺着没用、错误率飙升只有图上标注没人被通知；以及用户找不到面板入口。

| 项 | 内容 |
|---|---|
| 面板入口 | `scripts/ops_view.sh`：SSH 隧道 localhost:8200→服务器 8100，不暴露公文（边界仍在 nginx 只路由 /api/v1/*） |
| 磁盘 | hostmetrics.read_disk（statvfs 读 bind mount 的宿主盘；Windows/异常→None）；host chip 显示"盘 xx%"，>88% 变红 |
| 业务指标 | scraper 顺带提取 cache_hit_ratio{level}/browse_sync_lag_seconds 瞬时值 → topology business 块 → 面板"缓存 L1 x%/L2 x% · sync xs" chip |
| 错误率告警邮件 | watchdog 严重模块（既有阈值）→ 复用 guard 的 Go /internal/mail 通道一封汇总邮件；同模块 30min 冷却；标注不受邮件成败影响 |
| 验收 | i18 五项 + i9-i17 全回归 PASS + 浏览器 DOM 实测（缓存 chip 生产真值 L1 42%/L2 33%） |

**本轮两大事故（都修复+固化教训）**：
1. **scraper trust_env 缺失复活踩坑 #1**：`httpx.AsyncClient(timeout=10)` 没写 trust_env=False，Windows 系统代理劫持 127.0.0.1:8101 → 本地采集全 error（"curl 通 python 不通"同款）。生产 Linux 无代理侥幸无症状——**内网目标 httpx 必须显式 trust_env=False，不赌环境**
2. **scp 多源路径陷阱 + settings 环境差异双杀**：`scp -r app config/settings.yaml 目标/` 多源时 settings.yaml 落到项目根而非 config/——**I17 起 go_metrics job 从未注册过**（容器一直读镜像里 I8 旧 settings：enabled=false+8000），昨晚看到的"生产流量"全是本地开发隧道侧写入的（幸而同库同表，数据本身是真的）。发现后修正路径却把**本地 dev 值的 settings 整拷生产**（rag.qdrant_url 6333 隧道地址≠生产 25033）→ 生产 agent 启动失败循环重启 ~3 分钟 → sed 恢复 25033/13011 后痊愈。**教训：settings.yaml 是环境相关文件，生产部署只 scp app/，配置变更在服务器上就地改；宿主 config/ 目录挂载后，容器读的就是宿主文件**
3. 生产验证终态：scraper on 8080 首次真正注册、business 真值（L1 40.8%/L2 33.9%/sync 0s）、host+disk 82.4%、go_server 0.08 rps（业务流量深夜为 0 是真实情况，白天点亮）

---

# 第八轮循环 · I19 磁盘守卫与生产排障（2026-09-14 凌晨）

> 判词：磁盘 82.4% 是面板上线后暴露的第一个真问题；memguard 只看内存不看盘；维护脚本只清 dangling 收不走镜像大头。

| 项 | 内容 |
|---|---|
| 生产排障 | 磁盘 **82% → 74%**（释放 21G）：7 天前未使用镜像 8.8G（旧 tag 是大头，dangling 只占零头）+ build cache 9G + journal 848M + 旧 Go 日志/压测日志 gzip ~2G；"集市陈年代码" 16G 属用户归档资产，报告不动 |
| 维护脚本升级 | 服务器就地升级 docker-disk-maintenance.sh：镜像清理 dangling-only → `prune -af --filter until=168h`（7 天滚动保留回滚窗口）+ 7 天前 Go 日志自动 gzip |
| memguard 磁盘守卫 | `disk_verdict` 纯函数（selftest 4 断言）：>88% 且不在 6h 冷却 → disk_warning 上报；**只告警绝不自动删**（删什么永远人来定） |
| guard 适配 | kind=disk 全链：validate_report 分支校验（disk 事件无进程语义字段）/ 邮件体含 docker system df 建议 / 磁盘快析走标准模板不花 LLM / 主题无 MB 后缀 |
| 顺手修缺陷 | ①validate_report 缺字段抛 KeyError → endpoint 500 而非 422（测试抓到的真缺陷）②宿主机 python3.10 无 datetime.UTC——自包含脚本必须 timezone.utc 写法 |
| 验收 | i19 四项 + i17/i18 回归全绿 + 生产 E2E：disk 事件真发邮件、cron 干跑不误报（74%<88%）、公网 401 正常 |

**沉淀**：服务器侧排障动线——`docker system df` 看可回收 → `du -sh` 分层定位 → 区分"可机械回收"（镜像/缓存/日志压缩）与"人来定"（归档代码/数据库）；告警类守卫的处置边界：内存可阻断（进程可重启），磁盘不可自动删（数据不可逆）。

---

# R1（第 9 轮循环 · 20 轮纲领第 1 轮）：面板鉴权与账号体系（权限体系① · 2026-09-14）

> 总纲领启动轮。闭环五步执行记录（docs/OPS_ROADMAP_20.md 为 20 轮总图）。

**步骤 1 定问题/约束/量化目标**：面板 /ops/* 全开放仅靠"nginx 不路由"隐身（访问要 SSH 隧道），LLM 端点、遥测、标注全裸奔；目标公网可用 + 唯一账号 2215020897@qq.com + 后端鉴权链路禁改。量化：8 个 /ops/* 路由全鉴权、无凭证访问 100% 拒绝、登录链零新增生产依赖。

**步骤 2 三方案调研选型（Workflow 4 agent 并行：3 调研 + 1 裁决）**：
- A 直连密码登录（BFF 变体）：改动最小、单账号白名单收敛攻击面；con 是 AES 密钥复制与 7 天 JWT 不可吊销——BFF 化后全化解
- B OAuth2 授权码流：面板零密码接触最正统；但单用户机制超配（代码 2-3 倍 + 3-4 跳转 + 强依赖主站登录态 + PKCE 未知须实测）——留作未来加人升级路径
- C nginx auth_request：入口强制层（覆盖静态资源/未来路由/SSE），oauth2-proxy/Authelia 验证过的纵深；自身不解决凭证签发
- **裁决 A+C**：A 管 BFF 凭证（agent 代理登录→自签短时会话，后端 7 天 JWT 不出服务端）、C 管边缘执行。评分表存 R1 记录。
- 对裁决的两处有意偏离并说明：①密码加密放前端 WebCrypto（浏览器原生 AES-CBC 与主站同款）——agent 零加密代码零新依赖且不接触明文密码；②会话 TTL 12h 非 30min（单管理员日常重度使用；吊销语义保留换 secret 全量登出）

**步骤 3 业务定制**：email 白名单 404 不暴露 + JWT 验签白名单（HS256 固定算法/exp/UserID==278 双重）+ 登录防爆破（5 次/15min IP 锁定+恒定时间统一文案）+ login CSRF（Content-Type 强制 JSON + Origin 校验）+ 导航感知 302（text/html 请求 401 → 登录页，XHR 保持 401 JSON）

**步骤 4 架构落地**：app/ops/panelauth.py（无状态签名 cookie 会话 uid.exp.HMAC——多 worker/重启不丢，为 R9 铺路）+ main.py 四端点（/ops/login 页、/ops/auth/login 代理、/ops/auth 校验、/ops/auth/logout）+ 8 个 /ops/* 路由挂 Depends(require_panel) + 前端全局 fetch 401 拦截跳登录 + 登出按钮。生产 nginx：/ops/* 公网 + auth_request 强制层 + 豁免三件套 + 头清洗 + fail-closed（部署清单）

**步骤 5 风险兜底**：换 OPS_PANEL_SECRET = 全量登出（应急面）；agent 层+nginx 层双校验（边缘注入兜底）；后端不可达 fail-closed 统一文案；测试侧 tests/_ops_session.py 共享会话（存量 8 个测试文件自动带 cookie 回归）

**验收**：i20 六项全 PASS（免鉴权三件套/8 路由 401/登录链含 mock 后端与异 uid 403/伪造防护/防爆破状态机/WebCrypto 源码）+ i9-i19 全量回归绿 + 浏览器实测：未登录访问面板 302 登录页 → 表单真实登录（WebCrypto 加密→生产 Go→验签→BFF cookie）→ 42 节点画布 + 真实快照数据 ✓

**踩坑记录**：①WebCrypto IV 推导——`.slice(0,16)` 必须作用在 join 后的 hex 字符串（数组 slice 取的是 16 个"双字符元素"=32 字节，浏览器报 iv length 错）②本地 .env 的 JWT_SECRET 是开发自签值 ≠ 生产 Go jwtKey——完整登录链必须同步生产 secret③同账号连续登录后端偶发 500（refresh 保存冲突，禁改链路）——测试改 mock 后端，生产单管理员 12h 一登无此场景

---

# R2（第 10 轮循环 · 20 轮纲领第 2 轮）：流量分析台账（流量监控① · 2026-09-14）

**步骤 1 定问题/目标**：现状只有"最近两快照瞬时差分"——无历史回看、无窗口对比、无接口 TOP 榜；量化：30m/6h/24h 三窗口、TOP 榜、趋势、查询 <100ms、7 天保留、零新常驻服务。

**步骤 2 三方案调研（Workflow 4-agent）**：A-PG 时间桶 rollup（TimescaleDB caggs 手动版）/ B-TSDB sidecar（VictoriaMetrics/Prometheus）/ C-内存环形+JSONB 落盘（RRDtool 式）。**裁决 A（21/25）**：四硬约束全命中、错误口径聚合时固化、raw 2h 兼任重放源；B 违反零新基建纪律且能力过剩千倍；C 的内存直读是伪收益（A 已毫秒级）且 JSONB 锁死+重启重放是纯增险。

**步骤 3/4 落地**：app/ops/traffic.py——go_route_rollup_5m 表（PK bucket+method+route）、lag() 窗口函数差分（重置帧 dcount<=0 丢弃）、整桶 ON CONFLICT 全量替换（幂等可补算）、水位线重算"最近闭合桶+当前部分桶"、7 天滚动清理、60s APScheduler job；/ops/traffic API（白名单窗口）+ 面板"流量台账"侧滑面板（窗口切换/TOP 表格/CSS 趋势条——图表基座留给 R5）。

**步骤 5 风险**：counter 重置丢弃语义与 scraper.aggregate 一致；聚合在 raw prune(2h) 窗口内回溯无竞态；趋势条对空窗口诚实显示"暂无桶数据"。

**验收**：i21 五项（桶值精确/重置帧丢弃/401 不计错/幂等/窗口查询/422 契约/7 天清理/前端接线）+ i9-i20 全回归 + 浏览器实测（真实 55 接口 1314 请求 rps0.73、24h 切换、无状态 cookie 跨重启有效）+ 生产部署验证（55 接口/1333 请求/top1 checkUserVoted）。

**踩坑**：①asyncpg 对 `$1::interval` 参数做 interval 类型严格编码——'30 minutes' 字符串报 DataError，interval 常量改内联（值经白名单无注入面）；②SUM(bigint) 回 Decimal 与 float 运算报 TypeError——统一 float() 转换；③测试造帧用了"未来时间戳"被 ts<=now() 正确排除（生产语义对、测试数据错）——帧改用上一个完整 5m 桶；④asyncpg pool 绑首个事件循环——测试多 asyncio.run 撞车，重构单循环主协程。

---

# R3（第 11 轮循环 · 20 轮纲领第 3 轮）：错误捕获与指纹聚类（错误排查① · 2026-09-14）

**步骤 1 定问题/目标**：错误只有聚合计数——"哪个接口什么时候对谁报了什么错"答不了；量化：错误样本留存（request_id/IP/延迟/原始路径）、指纹去重（首末次/次数/影响面）、下钻视图、抗风暴（写入 O(指纹数)）、3 天滚动、Go 零侵入。

**步骤 2 三方案调研（Workflow 4-agent）**：A-Go 侧上报端点（Sentry SDK 式 push）/ B-日志侧 tail 采集（fail2ban+Fluent Bit+Sentry 指纹三合一）/ C-指标侧推断。**裁决 B（21分）**：A 违反 Go 零侵入+push 链路恰在风暴时最先过载被否；C 天然抗风暴但答不了"对谁/什么错"（其差分指纹与对账思想并入 B）；B 的关键前提是**Go 访问日志已是结构化 JSON 行**（client_ip/request_id/req_uri/status/latency_ns/time 全齐）。

**步骤 3/4 落地**：app/ops/errtail.py——60s tail job（与 go_rollup 错相）；路径归一化（数字→{id}/UUID→{uuid}/hex32→{hash}，对齐 /metrics route 标签防指纹过碎）；指纹=sha1(method+归一化route+status)（request_id/IP 绝不进指纹只进样本）；error_fingerprints UPSERT 计数全量 + error_samples per-指纹 60s 窗 5 条限流（抗风暴核心：写入与错误次数解耦）；checkpoint=(inode,size,offset) 落 PG（rename 轮转余量+新文件全量/截断防御）；解析失败率>5% 自监控 annotation（防格式变更静默失明）；新指纹自动 pprof goroutine 快照挂标注（10min 冷却）；3 天滚动清理。/ops/errors API + 面板"错误中心"侧滑（指纹折叠列表+样本表）。

**步骤 5 风险**：发现延迟 60s（定位为明细留存层，告警仍走 metrics 链路）；轮转竞态用 inode 撮合；agent 重启窗口漏采由 R2 对账兜底（R6 告警轮做正经对账）。

**验收**：i22 六项（parse_line/归一化指纹/限流计数分离/首跑增量轮转截断/清理查询/契约前端）+ i9-i21 回归绿（i21 修 top_n 被真实流量挤出的测试缺陷）+ 浏览器实测（空态诚实降级 + demo 数据完整渲染 + 截图）+ **生产上线即抓真数据**：9 条真实指纹，其中 `500 POST /api/auth/refresh ×6 影响 5 IP` 是真实生产 bug（此前散在日志无人知——错误中心第一枪立功）。

**踩坑**：①tail 同 inode 时"旧文件余量"与"当前文件增量"双重读取——重构为同 inode=增量/异 inode=轮转两分支；②"截断后新内容更长"读出半行→解析失败计数（业界 tail 同款边界，生产是 rename 轮转无此场景）；③py replace 脚本多次静默失配（转义差异），改用 Edit 工具直接编辑。

---

# R4（第 12 轮循环 · 20 轮纲领第 4 轮）：全链路请求追踪（故障溯源① · 2026-09-14）

**步骤 1 定问题/目标**：单请求"经历了什么"答不了（总耗时/状态码有，中间过程无）；量化：trace 采集零 Go 代码、request_id 可反查、慢请求榜、瀑布视图、Jaeger 不暴露公网。

**步骤 2 三方案调研（内联版，ultracode 关闭）**：A 部署 Jaeger 启用既有 OTel 管道（侦察发现 Go 已有完整埋点：tracing.go OTLP gRPC exporter + AlwaysSample + W3C TraceContext + request.id span 属性 + gorm metrics 插件——只是 collector 从未运行，exporter 一直在打空地址）/ B request_id 日志聚合（扩 R3 全量入库——存储负担+无 span 细节+nginx 无 rid 需模糊匹配）/ C SkyWalking/ELK APM（重基建违反零新纪律）。**裁决 A**：埋点现成是决定性优势，代价仅一个容器（memory 上限 300m，in-memory 存储重启即丢=观测窗口型数据可接受）。

**步骤 3/4 落地**：Jaeger all-in-one 1.57 容器（网络 sse_market_server_default，容器名=jaeger 使 Go env 里 OBSERVABILITY_JAEGER_ENDPOINT=jaeger:4317 直接解析，**Go 零改动零重启**——OTLP batch 自动重连）；查询端口 127.0.0.1:16686 回环发布（同 metrics 8080 模式）；agent app/ops/trace.py 只读查询代理（services/traces/by_request_id/trace_detail 四接口，归一化为瀑布数据：相对起点/时长 ms/路由/状态/rid）+ 诚实降级（available:false）；三路由 /ops/trace*（require_panel）；面板"链路追踪"侧滑（慢请求榜+瀑布详情）+ 错误中心样本 request_id 一键反查（交叉闭环：错误指纹→样本→trace）。

**步骤 5 风险与如实记录**：DB 层 span 缺失（gorm 插件仅 metrics）——升级路径 otelgorm 属 Go 改动留未来轮；nginx 层无 span（trace 起点=otelgin）；AlwaysSample 在当前流量（rps<5）无压力，流量上量需改采样率。

**验收**：i23 四项（归一化 fixture/四端点降级/API 契约/前端含反查链接）+ 生产实测：Jaeger 起后 trace 即流入（5 分钟 3 条真实用户请求：browse 13.1ms/updateBrowseNum 11.8ms/websocket 4.1ms），agent API 归一化输出正常；semconv 新旧 status 键名兼容。

---

# R5（第 13 轮循环 · 20 轮纲领第 5 轮）：可视化基座升级 + 首份评估报告（可视化运维① · 2026-09-14）

**步骤 1 定问题/目标**：图表各自为战（R2 CSS 条/R4 瀑布/G6 画布三套风格路径）；量化：统一图表入口一处定义色板/字体/空态、流量台账升级组合图、错误趋势图、首份 1000 字评估报告。

**步骤 2 三方案调研（内联）**：A ECharts 5 CDN（AntV 家族与 G6 同源、暗色主题、折线/柱状/热力全家桶）/ B Chart.js·uPlot（生态与中文社区弱、uPlot API 原始）/ C 手写 SVG 基座（用户明令禁止造轮子）。**裁决 A**——与 G6 引入模式完全一致（unpkg+jsdelivr 双源）。

**步骤 3/4 落地**：chartBase() 统一入口（CHART_INK/CHART_GRID 色板、textStyle/grid/tooltip 默认值、实例复用+resize 钩子）；流量台账 CSS 条退役 → 请求柱+错误率副轴折线组合图（tooltip/图例隐藏/柱圆角）；错误中心新增 24h 错误趋势图（errtail.query 补 trend 字段——rollup errors 桶序列，错误观测与流量台账同源）。

**步骤 5 风险**：CDN 断网退化（chartBase 返回 false 不白屏）；图表实例复用防侧滑重开泄漏；ECharts ~1MB gzip 后 ~330KB 一次性加载（与 G6 同模式）。

**验收**：i24 五项（基座/流量图表化/错误趋势/评估报告 1391 字四段齐/契约向后兼容）+ i12/i13/i20-i23 回归绿 + 浏览器实测 ECharts 真渲染（canvas 658x180）+ 截图 + 生产部署验证（108 桶趋势/9 桶有错）。

**里程碑（纲领 1-5 轮）**：首份使用者评估报告落盘 docs/OPS_EVAL_R1_R5.md（基于真实数据：refresh 500 真实 bug、55 接口台账、磁盘 82→74%、9 条错误指纹）。六条优化建议按优先级排入后续轮次。

**踩坑**：errtail.query 的 trend 查询误放连接池 with 块外（连接已释放）——挪回块内；SSH 隧道偶发中断重跑即绿。

---

# R6（第 14 轮循环 · 20 轮纲领第 6 轮）：告警规则引擎（自动化运维① · 2026-09-14）

**三方案（内联）**：A DB 规则表+agent 内评估（Alertmanager route/silence 手动最小版）/ B 部署 Alertmanager（多实例生态过重+文件化规则与面板 CRUD 冲突）/ C 纯代码规则（现状不可配）。**裁决 A**。

落地：ops_alert_rules 表（metric/op/threshold/for_cycles/severity/silence_min，seed 6 条：错误率/磁盘/内存/新指纹/对账偏差/任务停跳）+ ops_notifications 通知中心（已读语义）；评估引擎挂 watchdog 周期（持续累计+静默窗口状态机，纯函数可单测）；指标源七项（rollup 错误率·5m错误数、host 磁盘·内存、指纹新增、**日志侧 vs rollup 自动对账 gap**、job 心跳年龄）；warn/critical 加发邮件（复用 guard 通道），全部落面板通知中心；CRUD API 五端点；前端铃铛（未读 badge 60s 轮询）+ 通知/规则双 tab + 新增规则表单。附带 R9 前置的 selfmon 心跳模块（APScheduler listener 打点 → /metrics ops_job_stale_seconds gauge）。

验收：i25 五项（状态机/CRUD/通知流两轮触发→落库→ack/对账/前端）+ 回归 + 生产 6 条 seed 部署验证。
坑：①静默判定的假时钟必须用 epoch 量级（I19 同款教训第二次出现——固化：告警/冷却类测试时钟一律 1.7e9）；②heredoc 嵌 JS 模板字符串炸 bash——改 Write 文件+python 注入两步法。

---

# R8（第 15 轮循环 · 20 轮纲领第 8 轮）：前端工程化（架构优化① · 2026-09-14）

**三方案（内联）**：A 源码目录分域+Python 拼装构建（产物与拆分前功能等价、零语义漂移）/ B vite+npm（服务器无 node，构建产物与 A 等价但多一层工具链）/ C 维持单文件（1500 行临近维护边界）。**裁决 A**。

落地：frontend/ops/ 独立源目录（core.js 主流程+画布+侧栏+AI、traffic.js 含 R5 图表基座、errors.js、trace.js、alerts_actions.js、main.js + index.html 模板）；scripts/build_frontend.py 构建器（ORDER=依赖顺序内联、构建标记、--check 漂移检测可进部署流程）；一次性拆分器 split_frontend_once.py（按分区注释切割，跑一次后源目录为事实源）。真 ES Module 边界留待出现第二页面/构建需求时引入（当前全局脚本语义=零功能风险）。

验收：i27 四项（源结构/构建管线含漂移检测/完整性 function·onclick·listener 源=产物——I14 吞函数教训的自动化版/服务七功能入口）+ i9/i10/i11/i24/i25/i26 全回归 + 浏览器实测（42 节点/七函数/真实快照）。
坑：Windows 子进程 stdout GBK 编码 vs utf-8 解码崩（subprocess 需显式 encoding）；onclick 分布在模板 HTML+JS 模板串两处（完整性期望=两者之和）。

---

# R10（第 16 轮循环 · 20 轮纲领第 10 轮）：第一次全量生产化改造（生产稳定性①+ · 2026-09-14）

四维落地（全部生产验证，无文档层空转）：
- **安全加固**：/ops/* 响应头中间件（X-Frame-Options DENY / nosniff / Referrer-Policy / CSP 允 G6·ECharts 双 CDN）；登录成功/失败台账审计（panel_login 动作类型——此前登录无任何记录）
- **容灾**：pg_backup.sh 每日 4:10 备份（gzip 620K 实测，7 天滚动）+ crontab 挂载；**恢复演练真做**：dump→临时库 restore_drill 恢复 0 错误、四表行数全部对账一致（rollup 6367/errors 119/rules 6/actions 155）、RTO<1 分钟、演练库即清
- **性能**：rollup/错误样本 PK 与索引核查通过（窗口查询全部毫秒级，R2 设计红利）
- **架构收敛**：前端构建同步纳入验收（--check）；阶段评估报告 docs/OPS_EVAL_R6_R10.md（1033 字，R5 报告六条建议核销四条：告警自动化/动作面板化/前端工程化/备份演练）

验收：i29 五项（响应头含非 ops 排除/登录审计/备份链路 ssh 实查/报告字数/构建同步）+ i9/i20/i25/i26/i27/i28 回归 + 生产部署（响应头实测在，401 响应亦带头）。
坑：Windows 子进程管道 locale=GBK——中文与 ✓ 字符编码崩（stdout.reconfigure utf-8 固化；本轮第三次踩编码族坑，已入册：subprocess 必带 encoding+errors，脚本自 reconfigure）。

---

# R11（第 17 轮 · 20 轮纲领第 11 轮）：存储容量治理（弹性扩容① · 2026-09-14）

**三方案（内联）**：A 统一 TTL 框架 / B PG 分区表（<10 万行过度设计）/ C 降精度层级（RRD/Thanos 近细远粗）。**裁决 A+C**。

落地：go_route_rollup_1h 降精度表（90 天保留，从 5m 汇总、重算近 3 小时桶）；traffic 窗口扩 5 档（30m/6h/24h/**7d/30d**——长窗口走 1h 表）；error_samples 3→7 天（R5 建议核销）；ops_notifications 90 天清理（storage.cleanup_once 挂 watchdog）；/ops/storage 容量总览（6 表行数/保留策略/日增估算 1.63MB/90 天预估）；前端窗口按钮补 7 天/30 天。

验收：i30 五项（1h 层产出/长窗口契约/保留策略/容量总览/前端）+ i21/i25/i27 回归 + 部署。分区表如实记录为上量后升级路径。

---

# R12（第 18 轮 · 20 轮纲领第 12 轮）：开放标准互操作（架构优化② · 2026-09-14）

**三方案（内联）**：A 自暴露补完（HELP/TYPE 补全+文档）/ B Prometheus 容器抓双源（新常驻）/ C prometheus_client SDK（重依赖）。**裁决 A**（单管理员低频场景）。

落地：metrics.py 加 HELP 注册表（18 条）+ TYPE_HINT（counter 族）+ 无样本也输出 HELP/TYPE（采集端 schema 稳定）；docs/GRAFANA_INTEGRATION.md（prometheus.yml 片段含 Go Bearer 认证、10 条核心 PromQL 速查、最小面板 JSON）；histogram 升级路径（prometheus_client）如实记录。

验收：i31 五项（格式完整度每样本有 TYPE/HELP 18 条覆盖/counter _total 命名/Grafana 指南四要素/生产 18 条 HELP 实测）+ i9/i16/i29 回归 + 部署。

---

# R13（第 19 轮 · 20 轮纲领第 13 轮）：LLM 归因升级（故障溯源② · 2026-09-14）

**三方案（内联）**：A 假设+工具验证闭环（复用 ReAct 五只读工具）/ B 纯单次快析（幻觉）/ C 因果图（需标注数据）。**裁决 A**。

落地：app/ops/attrib.py——collect_facts（模块卡+24h 指纹+30m RED+30 天误报反馈四源事实包）→ LLM 生成 2-4 个假设（JSON 约束输出）→ 逐假设调确定性工具验证（search_routes/triage/module_card 真跑）→ 置信度三档（高≥2 验证/中=1/低=纯事实）→ 写回 annotation（source=llm_attrib）；误报反馈闭环（面板 ✓✗ → 台账 → 归因 prompt 注入降权）；30min 冷却防风暴。诊断报告面板加"LLM 归因"按钮（frontend/trace.js→build）。

验收：i32 五项（事实包/mock LLM 闭环 2/2 verified+置信度/反馈台账/API/冷却 force 穿透/前端）+ i26/i27 回归 + 部署。真实 LLM 路径同 ai.chat 回落链（glm-5.3→4.5）。

---

# R14（第 20 轮 · 20 轮纲领第 14 轮）：审计与账号安全（权限体系② · 2026-09-14）

**三方案（内联）**：A 复用 actions 台账加消费面 / B 独立审计服务 / C pgAudit。**裁决 A**——台账已全量记录四类动作，缺查询面。

落地：/ops/audit 查询 API（时间窗/动作类型过滤+类型分布统计）；通知中心加"审计"tab（动作类型 chip 筛选+时间线+result 颜色分级）；登录异常检测（login_failures_1h 指标 + seed 规则"登录暴力尝试" critical 30min 静默——R6 框架即插即用）。

验收：i33 五项 + i25/i27/i29 回归 + 部署。R10 登录审计（数据源）与 R6 告警（消费）在 R14 汇合为完整闭环。

---

# R15（第 21 轮 · 20 轮纲领第 15 轮）：SLO 与错误预算 + 第二份评估（可视化运维② · 2026-09-14）

**三方案（内联）**：A rollup 直算 / B 完整 error budget policy / C SLO 生成器。**裁决 A+B 元素**。

落地：ops_slo 表（seed 全局 99% + 核心路径 99.5%，面板可配）；错误预算计算复用 R11 的 1h 表（预算=(1-t)×req / 剩余 / 燃尽率 burn=err_rate/allowed）；slo_burn_rate_max 进 R6 告警框架（>2x warn，Google SRE burn rate 模式）；流量台账顶部 SLO 状态条（可用性/预算剩余/⚠️）；/ops/slo CRUD 三端点。**第二份评估报告 docs/OPS_EVAL_R11_R15.md**（1177 字，R10 五条建议核销三条）。

验收：i34 五项（CRUD/预算数学——生产实测 全局 99.873% burn 0.127/告警联动/前端/报告）+ i21/i25/i30 回归 + 生产部署验证（SLO 两条真值读出）。**里程碑 15/20 完成**。

---

# R16-R20 五轮终局（第 22-26 轮循环 · 2026-09-14 · 里程碑 20/20 完成）

## R16 故障自愈剧本库（自动化运维③ · 0915531）
三方案裁决 A。预案表关联告警规则名（trigger_rule→actions JSONB+auto/dry_run/cooldown）；L3 永不自动（安全底线）；告警触发联动（eval_once 中剧本结果附通知 detail——注意 INSERT 要在剧本后）；手动触发端点。seed 3 条（磁盘/停跳/登录暴力）。i35 五项。

## R17 攻击面收敛（安全加固① · 50e0a8d）
三方案裁决 A。CSP 去 unsafe-inline（构建时 sha256 hash 注入 meta——build_frontend.py 计算 script body hash，check() 同步修复）；/ops/* 全局 IP 限流 120/min（localhost IPv4/v6 双栈分流踩坑：限流按 IP 计数，localhost 交替解析两 IP 分计数——测试须强制 127.0.0.1）；密钥轮换脚本；frame-ancestors none。i36 四项。

## R18 水平扩展前兆（弹性扩容② · e22293b）
三方案裁决 A。PG 咨询锁 pg_try_advisory_lock（job_lock 上下文管理器+wrap_job APScheduler 闭包包装——直接传 wrap_job(fn,id) 返回协程对象会炸 Event loop，须闭包）；8 项无状态化审计；/ops/ha 就绪度 verdict=READY。i37 四项。

## R19 容灾与备份体系（生产稳定性② · 8661e6a）
三方案裁决 A。COS 异地推送（rclone/coscmd 双路径条件分支）；配置版本化（nginx/cron/memguard 三源）；六层降级预案文档；RPO 24h/RTO 5min。**生产事故 1 起**：部署命令 `echo "..." | crontab -` 是替换语义——crontab 只剩 1 条约 15 分钟（memguard/备份/清理/certbot 全丢），发现后恢复 6 条全量。教训固化：crontab 追加须 `(crontab -l; echo ...) | crontab -`。i38 五项。

## R20 第二次生产化+终局（本轮）
17 套全量回归全绿；docs/OPS_FINAL_REVIEW.md（终局复盘+长期运维方案：日常巡检/容量规划/升级流程/值班预案+遗留 6 条改进路径）；十维全覆盖确认（无半成品）；"全站运维 100% 依赖"达成度评估（八维闭环，最后一公里=使用习惯迁移）。
