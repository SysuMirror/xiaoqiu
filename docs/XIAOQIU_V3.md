# 小秋 v3：能力扩张与运营成熟度（2026-09-24）

锐评差距分析落地的实现轮。范围：论坛写侧/读侧扩容、注入防线、成本归因、
数据生命周期、开放 API、自动记忆抽取、发布工程。assist 接盘按用户裁决不做。

## 论坛写侧（create_post / create_comment L3 · toggle_like_post / toggle_vote_post L2）

**关键裁决：转发 Go API，不是直写 MySQL。** Go 的布隆中间件拦截
`/api/auth/showDetails|showPcomments`，直写的新帖 postID 不在布隆里（进程内存、
仅启动加载）→ 详情页 404。转发用户 JWT（compat ctx 注入 `auth_header`；
Go `requireViewerLegacyPhone` 实际只看 JWT，见 controller/viewer.go:24）后，
布隆/多级缓存/站内通知/排行联动/raghook 全部由 Go 自己保证一致。

安全链：配置开关（`forum_write.enabled`，图注入同开关）→ 禁言检查 → 内容审核
（moderation 词库粗筛 + LLM 复核，仅发帖/评论）→ 每日限频（PG 台账计数）→
L3 强制人审（interrupt，用户回复「同意」；compat 审批闭环零前端改动复用）→
执行 → 台账 + `agent_forum_writes_total`。分区白名单 11 个；帖 ≤30 字标题（Go 同款）。

## 读侧扩容（L1）

`sql_post_comments` / `sql_my_posts`（config 注入身份）/ `sql_hot_posts`（heat 排序）/
`sql_board_stats`（分区热度）。沿用私密帖不可见 + SHA256 别名基线。

## 注入防线（app/security/injection.py）

检索切片与 UGC 是数据不是指令：kb_search 出口逐条标注 `[不可信内容]`；
SQL 工具命中注入特征加整块免责脚注；系统提示新增数据/指令边界声明。
指标 `agent_injection_hits_total{source}`。i48 T1 验证中英特征与不误伤。

## 成本归因 + 首 token SLO

runs 表加 `tokens_total / first_token_ms / model`（幂等迁移）；compat 流式路径
逐 run 累计（updates 模式取 `token_used`），成功时落库。
`GET /api/v1/agent/me/usage?days=30` 按日用量；指标
`agent_first_token_seconds_total/_count{model}`（首 token 时延 SLO 数据源）。

## 数据生命周期

- 每日 04:17 retention job：软删会话 N 天物理清除（checkpoint 三表）、runs 90d、
  actions 365d。幂等，台账留痕。
- `GET /api/v1/agent/me/export`：会话+记忆+反馈+用量 JSON 下载。
- `POST /api/v1/agent/me/delete`：两步确认（confirm_token 10min）；清
  threads/checkpoints/记忆(含向量)/反馈/runs；台账按审计要求保留但剥离身份与内容。
- 反馈工单化：负反馈实时进面板通知中心（`message_feedback` severity=warn）；
  `GET /api/v1/agent/feedback/list`（admin）。

## 开放 API（能力输出）

`POST /api/v1/agent/open/kb_search`，`X-API-Key` 鉴权（PG `openapi_keys` 哈希存储），
限频 30/min + 按日配额。签发：

```bash
ssh SSE_Market 'docker exec sse_agent python -c "import asyncio;from app.api.openapi_service import issue_key;print(asyncio.run(issue_key(\"项目名\",\"负责人\")))"'
```

## 自动记忆抽取

run 正常结束后后台 LLM（glm）从本轮问答抽 0-2 条稳定事实 → 复用 save 合并管线。
只喂用户消息+最终回答（不喂检索内容，防注入污染记忆库）；每用户冷却 30min。

## 发布工程

- `scripts/deploy.sh`：备份远端 app/ → scp（tar 排除 pycache）→ restart → 健康等待
  → metrics/日志冒烟 → 失败自动回滚。settings.yaml 铁律不变（永不 scp，就地改）。
- `scripts/ci.sh`：改动文件 ruff + 全量编译 + 权限矩阵一致性。
- admin 加固：`admin.uid_allowlist` 非空时强制白名单（deps.py）。

## 验收

tests/i48_forum_write.py：T1-T2 单元（注入/矩阵）；--live 时 T3 开放 API 鉴权、
T4 发帖审批链（interrupt→同意→落库→清场）。golden case 新增 post_draft_confirm。

## 遗留 / 明确不做

- 点赞/投票无 L3（可逆，L2 + 台账）；评论关注者邮件通知由 Go 在转发链路自动完成
- 首 token 时延护栏只做观测+告警数据源，未做超时降级（图内流式包装风险大，记录方向）
- 多模态：模型池无 VL 模型，待网关扩容；PWA/推送需主站壳配合
- 信任域隔离（JWT_SECRET 与主站分离）需主站配合签发机制，本轮只做 admin 白名单加固
