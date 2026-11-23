# 统一 RAG 服务协作建议（小秋侧反馈）

> 提给 RAG 组同事的改进建议。当前对接状态：`/kb/search` 已在小秋生产链路上（market 源），
> 服务地址：集市机 `http://127.0.0.1:13019`（frp 直通）/ 公网 `https://rag.ssemarket.cn`。
> 按影响排序，前四条建议优先。

## P0 建议项

### 1. 加鉴权或至少加来源限制
现状无鉴权，公网可任意调用。/kb/search 是纯只读还好，但 /qa/ask 会消耗组内 LLM 配额，
被脚本刷会烧穿资源包（小秋侧有过 assist 缓存层把 qwen key 烧到 402 的前车之鉴）。
最小改法：请求头 API Key（随机 32hex 即可）+ 简单 IP 白名单（集市机/内网网段）；
不必上完整 OAuth。Key 通过线下同步，放各自 .env。

### 2. /qa/ask 支持（或明确声明不支持）多轮 session
现状 `session_id` 传入但（小秋侧观测）未见上下文生效。如果目标是"检索+单轮回答"，
建议从文档中删掉 session_id 参数避免误解；如果要做多轮，建议明确 TTL 与历史轮数上限，
并在响应里回 `history_used` 便于调用方验证。

### 3. 返回切片的元数据（metadata）
现在只有 `source/content/rank/score`。强烈建议每个结果附：
- `title`（帖子标题，现在要靠解析 content 首行"标题："）
- `post_time`（时间衰减/时效判断靠它）
- `chunk_id` / `chunk_seq`（切片序号，调用方做去重与聚合）
- `author_type`（是否匿名帖）
有了这些，调用方才能做"最新优先/加权融合"，而不是只能按 rank 顺序吃。

### 4. /kb/search 支持 k>5（或批量）
k 上限 5 对"先宽召回再精排"的两段式用法不够。建议：
- 放宽到 k≤20（成本可控，检索本身便宜）；或
- 提供 `recency` / `time_range` 过滤参数（如 `after=2026-01-01`），
  时效类问题不用靠大 k 硬捞。

## P1 建议项

### 5. score 语义文档化 + 归一化
`score=1/rank` 只是相对排序值，文档已说明，很好。建议更进一步：
- 响应里加 `retriever: "bge-m3+rrf"` 之类的版本字段，未来换模型调用方可感知；
- 若能输出真实相似度（cosine）+ 相对分数双字段更好，调用方好设"无结果"阈值。
  小秋侧现在用"结果数为 0"判空，没有分数阈值可用。

### 6. 健康检查端点
加 `GET /healthz`（不查库，进程活着即可）与 `GET /readyz`（查一次真实检索）。
调用方（小秋/其它 agent）做降级判断需要它；502 重试我们已在做，但无法主动探活。

### 7. 流式输出（/qa/ask）
若 /qa/ask 定位是面向 C 端的问答，建议支持 SSE 流式（LLM 首 token 7-17s，
非流式体感很差）。/kb/search 无需。

### 8. category=market 数据的更新频率声明
market 源和集市主库的同步机制/延迟是多少（实时 webhook / 定时批量）？
建议文档写明"新帖可见延迟"，调用方回答用户"我刚发的帖子怎么搜不到"时需要这个事实。

### 9. 错误码细化
现在 400/422/502 三种。建议 502 细分：上游 LLM 失败（qa）/ 检索库超时 / 内部异常，
并都返回 `{"error": {"code": "...", "message": "..."}}` 结构化体，便于重试策略区分
（检索超时可重试，LLM 配额烧穿重试无意义）。

## 已对接细节备查（小秋侧实现）

- 客户端：`httpx` 直连，`trust_env=False`（避开 Windows/容器代理劫持内网域名）
- 超时 8s，502 重试一次；失败降级到小秋自建 qdrant 链路（g3dfc5d61_ssemarket_rag_v2）
- `source=market_<postID>.txt` → 反查 `https://ssemarket.cn/new/postdetail/<postID>`
- content 解析：首行 `标题：` 提取标题；正文截 180 字符进 LLM 上下文
- 代码位置：SSE-Agent 仓库 `app/rag/unified.py`（客户端）/ `app/tools/chat/kb_search.py`（工具接线）
