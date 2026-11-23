# SSE-Agent · 小秋（chat + 论坛前厅）

> SSE Market 论坛的对话 Agent 与论坛前厅。ops 面已下线，运维由 Skein 控制台/Agent 接管。
> 本仓库不再包含 ops 面板、主机巡检、告警、kill/审批等运维能力。
>
> 与雁阵的接口见 Skein 仓 `docs/assistants.md`；镜像 `xiaoqiu:<ver>-skein-frontdesk`（构建见 `docker/Dockerfile.skein-frontdesk`）。

小秋现在只做两件事：

1. **chat 面**：`/task/sse`，论坛/知识库助手——检索、统计、引用溯源、记忆、反馈。
2. **论坛前厅**：`app/frontdesk.py` 轮询帖子/评论/楼中楼，所有吐槽都回；bug/需求用助手
   凭证经雁阵的公共 API 建成工单，验收后雁阵用带签名的回调在原帖回写公开状态。

## 核心特性

| 能力 | 说明 |
|---|---|
| 多轮对话 | SSE 流式（token/工具进度/审批卡片事件），断连宽限 90s 不丢结果 |
| 论坛问答 | 模板化 SQL（防注入/防 DoS/脱敏）+ 向量语义检索 |
| 引用溯源 | 知识类回答强制附帖子链接；检索不到明说，不编造 |
| 行动台账 | append-only：自主操作可追溯，对话中可被问询 |
| 记忆治理 | 写时合并 + hash 去重 + 容量/TTL |
| 论坛写侧 | L3 人审：create_post / create_comment 经用户「同意」后执行 |
| 前厅闭环 | 帖子/评论/楼中楼回执、建单、Skein 状态回写 |
| 可靠性 | run 状态机 + 启动孤儿对账 + 并发上限 + 单 run token 预算 + LLM 熔断回退 |
| 评测 | golden cases 回归（手动跑，JSON 报告看趋势） |

## 快速开始（开发）

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
cp .env.example .env        # 填 secrets
bash scripts/dev_tunnel.sh &
bash scripts/restart_server.sh
python tests/sse_client.py "论坛最新3条帖子" t-demo
```

## 目录

```
app/          chat + 前厅（FastAPI + LangGraph）
  frontdesk.py  论坛前厅 monitor
  api/          chat/前厅/RAG 路由
  memory/ RAG/ tools/  graph/
tests/        单元与验收脚本
docker/       镜像构建
docs/         部署与架构文档
```

## 文档

- [docs/DEPLOY.md](docs/DEPLOY.md) — 部署手册（chat/前厅、nginx SSE、镜像）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构基准（历史 ADR；ops 章节已过时）
- [docs/README.md](docs/README.md) — 文档地图

## 测试

```bash
pytest tests/test_frontdesk.py tests/test_skein_trace.py tests/test_skein_user.py
```

完整验收脚本见 `tests/`；依赖 qdrant/langgraph 的用例在生产镜像内跑。
