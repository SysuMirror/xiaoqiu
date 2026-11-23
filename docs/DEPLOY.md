# 部署手册（SSE_Market 单服务器）

> 小秋只部署 chat + 论坛前厅；ops 面已下线，运维由 Skein 控制台/Agent 接管。

## 前置

| 组件 | 位置 | 说明 |
|---|---|---|
| agent 专用 Postgres | 容器 `sse_agent_pg`（pgvector/pgvector:pg16） | `127.0.0.1:5433`，user/pass `agent`，库 `sse_agent`，**只绑 127.0.0.1** |
| 论坛 MySQL | 容器 `sse_market_db` | `3506` 端口，库 `ssemarket`（只读使用） |
| embedding 模型 | 本地 `paraphrase-multilingual-MiniLM-L12-v2` | 首次运行自动下载（HF 镜像） |

## 服务器上部署

```bash
cd /root/SSE_Market/SSE-Agent/sse-agent   # 代码目录
cp .env.example .env                      # 填 secrets：LLM key / DB DSN / JWT_SECRET(>=32字节) / SMTP
docker compose -f docker/compose.yml up -d --build
docker logs -f sse_agent
curl -s localhost:8100/health/ready       # {"status":"ok"}
```

首次：灌 RAG 索引（本机 python 或容器内执行）：

```bash
docker exec sse_agent python -m app.rag.sync --limit 300
```

## nginx（SSE 必配，否则流式会被缓冲/超时）

```nginx
location /agent/ {
    proxy_pass http://127.0.0.1:8100/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;                    # 关键：SSE 不缓冲
    proxy_read_timeout 300s;                # 长回复/工具调用等待
    add_header X-Accel-Buffering no;
}
```

## 邮件切换到真实 SMTP

`config/settings.yaml`：

```yaml
mail:
  mode: smtp            # dev 是 log（落盘 var/mail_outbox）
  recipients:
    admins: 真实邮箱@xxx.com
```

`.env` 加 `SMTP_USER` / `SMTP_PASS`（163 用授权码）。切换后跑
`POST /admin/jobs/digest/run {"force": true}` 验证收信。

## 例行动手项

- 每日备份：`docker exec sse_agent_pg pg_dump -U agent sse_agent | gzip > backup/$(date +%F).sql.gz`（cron）
- 评测：改 prompt/换模型后跑 `python -m app.eval.run_eval`，看 `eval/reports/` 趋势

## 已知信任边界（ADR-6）

agent 容器持有 docker 控制能力（经 SSH 或本机 docker.sock），约等于 root。
缓解：容器名白名单 + L3 人工审批 + 行动台账。彻底解（独立低权限 executor）见 ARCHITECTURE.md §18。

## 论坛前厅 monitor（Skein 闭环）

小秋 chat 容器内可运行 `app/frontdesk.py`：轮询论坛新帖/新评论，所有吐槽都回，
bug/需求用助手凭证调雁阵的公共 API（`POST /api/v1/tickets`）建单；雁阵验收后用带签名的
回调调本服务 `POST /internal/skein/notify`，由 `app/api/internal.py` 以 `XQ_BOT_UID`
身份回原帖。

- 开关：`FRONTDESK_ENABLED=1`；间隔/回看/限频：`FRONTDESK_INTERVAL`（60）、
  `FRONTDESK_LOOKBACK`（600）、`FRONTDESK_MAX_PER_HOUR`（12）。
- 分类模型：`FRONTDESK_MODEL` + `FRONTDESK_LLM_BASE`（当前 DeepSeek）。
- 一级评论和楼中楼（ccomments）都监测；评论一律交给 LLM 判断。同帖已有在途工单时，
  小秋不再重复建单（按帖去重，reply 里带原工单编号）。
- 楼中楼回执带 `reply_ccomment_id`，用 `ReplyCcommentID` 嵌套在原回复下。
- 雁阵（助手接入）：`SKEIN_CONTROL_URL` + `SKEIN_ASSISTANT_KEY`（助手凭证：只能建单、
  查自己的单、上报模型调用留痕）+ `SKEIN_WEBHOOK_SECRET`（校验雁阵的状态回调）；
  **不再持有雁阵的主令牌**。主站：`GO_INTERNAL_URL` + `INTERNAL_TOKEN`；机器人账号：
  `XQ_BOT_UID`。
- 镜像：`docker/Dockerfile.skein-frontdesk`（直接用当前仓库 `app/` 覆盖基础镜像，并移除旧 ops 代码）。
- 测试：`pytest tests/test_frontdesk.py`。

完整接口契约见 Skein 仓 `docs/assistants.md`。
