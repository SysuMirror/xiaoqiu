# 小秋客户端生产化改造（v2）——契约与架构说明

> 2026-09-24 已部署生产并 E2E 验收（本地 + 生产各 14/14 PASS）。
> 目标：对标豆包级客户端——会话中心 / 语义记忆 / 真流式 / 消息反馈 / 开场建议。

## 一、架构变化总览

图仍是 `prep → agent → tools` ReAct（三节点），升级全部在数据层与 API 层：

| 能力 | v1（玩具态） | v2（现在） |
|---|---|---|
| 流式 | ping 心跳 + 结束一次性 final | **真流式**：append 逐 token + status 工具事件 + final 校准 |
| 会话 | 仅"当前 session"，刷新即丢 | 会话列表/改名/删除/切换/自动标题，bootstrap 恢复最近会话 |
| 记忆 | "最近 5 条"按时间注入 | **语义召回**（bge-m3 → qdrant g3dfc5d61_xiaoqiu_memory）∪ 最近，去重注入；用户可见可删可改 |
| 知识检索 | 本机 qdrant 直查 | **统一 RAG 服务**（127.0.0.1:13019）优先，旧链路降级兜底 |
| 反馈 | 无 | 消息级赞/踩（绑 run_id），PG 落库，可评报统计 |
| 开场 | 固定欢迎语 | 热帖驱动的建议 chips |

## 二、SSE 事件契约（/api/v1/agent/task/sse，schema_version 2）

```
event: ping     {schema_version:2}                                  # 心跳，15s
event: content  {schema_version:2, mode:"append", text}             # 最终回答逐 token
event: status   {tool:"kb_search", args:"query=..."}                # 工具调用（前端渲染步骤条）
event: content  {schema_version:2, mode:"final", text, run_id}      # 全文校准 + run_id
event: error    {code, message, request_id}
```

- append 的文本若后跟工具调用，前端把已流出部分折叠为步骤，final 永远是权威全文
- **run_id 只在 final 事件携带**，前端赞/踩反馈绑定它
- 审批流（interrupt → "回复同意/拒绝"）语义不变

## 三、新增 API（全部 JWT Bearer，按 user_id 隔离）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/v1/agent/conversations?limit&offset | 会话列表（updated_at 倒序，含自动标题） |
| GET | /api/v1/agent/conversations/{id}/messages | 任意历史会话消息（≤200 条，属主校验 403） |
| POST | /api/v1/agent/conversations/{id}/title | 重命名（≤60 字） |
| DELETE | /api/v1/agent/conversations/{id | 软删；复用返回 410 |
| GET | /api/v1/agent/memory | 记忆列表（最新在前） |
| PUT | /api/v1/agent/memory/{key} | 改写记忆（重算向量） |
| DELETE | /api/v1/agent/memory/{key} | 删除记忆（事实源+向量镜像同删） |
| POST | /api/v1/agent/feedback | {run_id, thread_id, rating:1/-1, comment?} 重复提交为覆盖 |
| GET | /api/v1/agent/feedback/stats?days | 赞/踩统计 |
| GET | /api/v1/agent/suggestions | 开场建议（3 热帖 + 3 固定，最多 6 条） |

**bootstrap 变更**：GET 不带 session_id 时**恢复用户最近会话**（豆包行为），
无历史才发新 session；响应新增 `title` 字段。前端"新对话"应生成新 id 走 POST。

## 四、数据层

- `threads` 表新列：`title / updated_at / deleted`（软删）+ `(user_id, updated_at DESC)` 部分索引；
  首轮问答后异步生成 ≤12 字标题（LLM，失败回落截断首问 24 字）
- `message_feedback` 新表：`(run_id, user_id)` 唯一，赞/踩可覆盖更新
- 记忆双写：LangGraph PostgresStore 为事实源 + qdrant `g3dfc5d61_xiaoqiu_memory`
  （1024 维 bge-m3，payload: user_id/key/text/updated_at）；召回 = 向量 top5(score≥0.35) ∪ 最近 5，去重
- 向量链路任何故障自动降级为"按时间取最近"，不阻断对话

## 五、前端（服务器仓库 sse_market_new_client/newSSE）

- `api/opsagent/`：conversation.ts（扩展）、memory.ts、suggestions.ts（含反馈提交）
- `composables/useOpsAgentStream.ts`：status 事件 → 工具步骤卡；final 捕获 runId
- `components/smart-xiaoqiu/`：ConversationSidebar.vue（列表/重命名/两击确认删除/移动端抽屉）、
  MemoryDrawer.vue（记忆查看/编辑/删除）、SmartXiaoQiuTimeline.vue（消息赞踩行）
- `views/SmartXiaoQiuView.vue`：侧栏布局（桌面常驻/移动抽屉）、建议 chips、记忆入口
- **部署 = 仓库内 `npm run build`**（dist/ 直接 bind-mount 给 nginx：`dist → /usr/share/nginx/new`）

## 六、已知限制 / 后续

1. 自动标题偶尔回落截断文本（glm 现强制思考，短 max_tokens 下输出质量不稳）——待智谱行为稳定后调
2. 记忆写入仍依赖 agent 主动调用 memory_save，无自动抽取（避免写放大与误记）
3. web_search 默认关（计费资源），开启后 status 步骤条自动覆盖
4. glm 思考参数教训（2026-09-24 实测）：**5.3 与 4.5 均强制思考**，传 disabled/low 都被 1210 拒，
   唯一正确姿势是不传 thinking 字段；ops ai.py 与标题生成两处已统一
5. E2E：`scripts/xiaoqiu_client_e2e.py`（`XQ_E2E_BASE=https://ssemarket.cn` 打生产；
   假用户 u9527x，跑完自动清场）

## 七、验收记录

- 本地（隧道 + dev server）：14/14 PASS（含真流式 36 append/2.5s）
- 生产（ssemarket.cn 全链路经 nginx）：14/14 PASS（18 append，标题生成、记忆召回、反馈统计全绿）
- 附带修复：JWT 校验加 leeway=5（客户端时钟偏移导致的 ImmatureSignatureError 间歇 401）
