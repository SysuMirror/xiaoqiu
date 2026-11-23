"""论坛写侧工具（v3）：经用户 JWT 转发 Go 写 API，L3 人审后执行。

为什么转发 Go API 而不是直写 MySQL：Go 的布隆中间件拦截 /api/auth/showDetails，
直写的新帖 postID 不在布隆过滤器里（进程内存，仅启动时从库加载）→ 详情页 404。
转发后布隆/多级缓存失效/站内通知/排行联动/raghook RAG 同步全部由 Go 自己保证一致。
Go 侧 requireViewerLegacyPhone 实际忽略 phone 参数、只看 Authorization JWT（viewer.go:24），
因此透传用户请求的 Authorization 头即可代表用户执行。

安全链（每个工具）：配置开关 → 身份只来自 ctx 注入的 JWT → 禁言检查 →
内容审核（词库粗筛 + LLM 复核，仅发帖/评论）→ 每日限频（PG 台账计数）→
registry L3/L2 审批语义 → Go API → 台账 + metrics。
"""
from __future__ import annotations

import logging

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.config import get_config
from app.memory import ledger
from app.observability import metrics

log = logging.getLogger(__name__)

GO_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def _cfg(config: RunnableConfig) -> dict:
    return (config or {}).get("configurable") or {}


def _uid(user_id: str) -> int:
    raw = (user_id or "").strip()
    return int(raw[1:]) if raw[1:].isdigit() else 0


def _auth(config: RunnableConfig) -> str:
    auth = str(_cfg(config).get("auth_header", "") or "")
    return auth if auth.lower().startswith("bearer ") else ""


async def _go_post(path: str, payload: dict, config: RunnableConfig) -> tuple[bool, str]:
    """转发 Go 写 API。返回 (成功, 消息)。Go 成功 = HTTP 200 且 code==200。

    地址：docker.sock 动态解析容器 IP（兜底容器重建丢端口发布的回归），失败回落配置。"""
    cfg = get_config().forum_write
    auth = _auth(config)
    if not auth:
        return False, "未获取到登录凭证，无法代为执行写操作"
    from app.common.goaddr import resolve_go_base

    base = await resolve_go_base(get_config().go_backend.container_name) or cfg.base_url
    try:
        async with httpx.AsyncClient(timeout=GO_TIMEOUT, trust_env=False) as hc:
            resp = await hc.post(f"{base.rstrip('/')}{path}", json=payload,
                                 headers={"Authorization": auth})
    except Exception as e:  # noqa: BLE001
        log.warning("forum write %s failed: %s", path, e)
        return False, "论坛服务暂不可用，操作未执行"
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return False, f"论坛服务响应异常（HTTP {resp.status_code}）"
    code = data.get("code")
    msg = str(data.get("msg", ""))[:120]
    # Go 写 API 两种成功形态：/api/auth/post 回 {code:200}；
    # /api/auth/postPcomment 成功直接回业务对象（无 code 字段）——code 缺失
    # 且 HTTP 200 = 成功（修：此前把裸对象成功误报为"code 是空的"失败）
    if resp.status_code == 200 and code in (None, 200):
        return True, msg or "成功"
    return False, msg or f"论坛拒绝了该操作（code={code}）"


async def _precheck(config: RunnableConfig, action_type: str) -> str:
    """公共前置：开关/身份/禁言/限频。返回空串=通过，否则为拒绝原因。"""
    fw = get_config().forum_write
    if not fw.enabled:
        return "发帖能力未开通"
    user_id = str(_cfg(config).get("user_id", ""))
    uid = _uid(user_id)
    if uid <= 0:
        return "无法识别用户身份"
    from app.moderation import is_banned

    if await is_banned(uid):
        return "该账号处于禁言期，无法执行写操作"
    limit = {"create_post": fw.daily_post_limit, "create_comment": fw.daily_comment_limit,
             "toggle_like_post": fw.daily_like_limit, "toggle_vote_post": fw.daily_vote_limit,
             }[action_type]
    label = {"create_post": "发帖", "create_comment": "评论",
             "toggle_like_post": "点赞", "toggle_vote_post": "投票"}[action_type]
    used = await ledger.count_today(action_type, user_id)
    if used >= limit:
        return f"今日{label}次数已达上限（{limit}/日）"
    return ""


async def _content_check(text: str) -> str:
    """发帖/评论内容审核：词库粗筛命中才 LLM 复核（与 moderation 同一宁漏不误取向）。"""
    from app.moderation import PREFILTER, judge

    if not PREFILTER.search(text[:300]):
        return ""
    verdict = await judge(text)
    if verdict and verdict.get("abusive") and verdict.get("severity") == "explicit":
        return "内容未通过安全审核，请修改后重试"
    return ""


def _finish_ok(config: RunnableConfig, action_type: str, target: str, detail: str) -> None:
    metrics.inc("agent_forum_writes_total", {"tool": action_type, "result": "success"})
    _spawn_ledger(config, action_type, "success", target=target, detail=detail)


def _finish_reject(config: RunnableConfig, action_type: str, reason: str) -> str:
    metrics.inc("agent_forum_writes_total", {"tool": action_type, "result": "rejected"})
    _spawn_ledger(config, action_type, "rejected", detail=reason[:200])
    return f"操作未执行：{reason}"


def _finish_failed(config: RunnableConfig, action_type: str, detail: str) -> str:
    metrics.inc("agent_forum_writes_total", {"tool": action_type, "result": "failed"})
    _spawn_ledger(config, action_type, "failed", detail=detail[:200])
    return f"发布失败：{detail}"


_bg_tasks: set = set()


def _spawn_ledger(config: RunnableConfig, action_type: str, result: str,
                  target: str = "", detail: str = "") -> None:
    """fire-and-forget 台账（持有引用防 GC，同 moderation.spawn_moderation 模式）。"""
    import asyncio

    cfg_ctx = _cfg(config)

    async def _run():
        await ledger.record(str(cfg_ctx.get("user_id", "")), action_type, result,
                            target=target, detail=detail,
                            thread_id=cfg_ctx.get("thread_id"))

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@tool
async def create_post(
    title: str,
    content: str,
    partition: str,
    tag: str = "",
    is_anonymous: bool = False,
    config: RunnableConfig = None,
) -> str:
    """以当前用户身份在论坛发布一篇帖子（需用户回复「同意」确认后才会真正执行）。

    调用前必须把最终稿（标题/正文/分区）完整展示给用户确认；用户同意后再调用本工具。
    不要使用 @提及 语法。适合用户明确说"帮我发个帖/把这段发出去"的场景。

    Args:
        title: 帖子标题，最多 30 字
        content: 帖子正文，用户确认过的最终稿，不要自行扩写
        partition: 分区，只能是：生活日常/主页/学习交流/技术分享/课程专区/实习就业/社团活动/宿舍生活/组团捞人/期末资料/其他
        tag: 标签，单个词，可留空
        is_anonymous: 是否匿名发布，默认否
    """
    fw = get_config().forum_write
    reason = await _precheck(config, "create_post")
    if reason:
        return _finish_reject(config, "create_post", reason)
    title = (title or "").strip()
    content = (content or "").strip()
    partition = (partition or "").strip()
    if not title or len(title) > fw.max_title_chars:
        return _finish_reject(config, "create_post", f"标题需 1~{fw.max_title_chars} 字")
    if not content or len(content) > fw.max_content_chars:
        return _finish_reject(config, "create_post", f"正文需 1~{fw.max_content_chars} 字")
    if partition not in fw.partitions:
        return _finish_reject(config, "create_post", f"分区必须是 {'/'.join(fw.partitions)} 之一")
    reason = await _content_check(f"{title}\n{content}")
    if reason:
        return _finish_reject(config, "create_post", reason)

    ok, msg = await _go_post("/api/auth/post", {
        "title": title, "content": content, "partition": partition,
        "photos": "", "tagList": (tag or "").strip()[:50],
        "UserTelephone": "xiaoqiu-agent",
        "isPrivate": False, "isAnonymous": bool(is_anonymous),
    }, config)
    if not ok:
        return _finish_failed(config, "create_post", msg)
    post_id = await _latest_post_id(config)
    _finish_ok(config, "create_post", f"post:{post_id or '?'}", f"标题：{title[:60]}")
    link = f"https://ssemarket.cn/new/postdetail/{post_id}" if post_id else "论坛首页"
    return f"已发布（用户确认后执行）。链接：{link}"


async def _latest_post_id(config: RunnableConfig) -> int:
    from app.tools.forum.sql_templates import _query

    uid = _uid(str(_cfg(config).get("user_id", "")))
    if uid <= 0:
        return 0
    _, rows = await _query(
        "/*T*/ SELECT postID FROM posts WHERE userID=%s ORDER BY postID DESC LIMIT 1", (uid,))
    return int(rows[0][0]) if rows else 0


@tool
async def create_comment(post_id: int, content: str, config: RunnableConfig = None) -> str:
    """以当前用户身份在指定帖子下发布一条评论（需用户回复「同意」确认后才会真正执行）。

    调用前把评论文本完整展示给用户确认。不要使用 @提及 语法；评论最多 1000 字。

    Args:
        post_id: 目标帖子的 postID（数字）
        content: 评论文本，用户确认过的最终稿
    """
    reason = await _precheck(config, "create_comment")
    if reason:
        return _finish_reject(config, "create_comment", reason)
    content = (content or "").strip()
    if not content or len(content) > 1000:
        return _finish_reject(config, "create_comment", "评论需 1~1000 字")
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return _finish_reject(config, "create_comment", "postID 必须是数字")
    if post_id <= 0:
        return _finish_reject(config, "create_comment", "postID 无效")
    reason = await _content_check(content)
    if reason:
        return _finish_reject(config, "create_comment", reason)

    ok, msg = await _go_post("/api/auth/postPcomment", {
        "UserTelephone": "xiaoqiu-agent", "PostID": post_id, "Content": content,
    }, config)
    if not ok:
        return _finish_failed(config, "create_comment", msg)
    _finish_ok(config, "create_comment", f"post:{post_id}", content[:100])
    return f"评论已发布到帖子 {post_id}（用户确认后执行）。"


@tool
async def toggle_like_post(post_id: int, config: RunnableConfig = None) -> str:
    """以当前用户身份对指定帖子点赞或取消点赞（切换语义，与站内行为一致）。可逆操作。

    Args:
        post_id: 目标帖子的 postID（数字）
    """
    reason = await _precheck(config, "toggle_like_post")
    if reason:
        return _finish_reject(config, "toggle_like_post", reason)
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return _finish_reject(config, "toggle_like_post", "postID 必须是数字")
    if post_id <= 0:
        return _finish_reject(config, "toggle_like_post", "postID 无效")

    ok, msg = await _go_post("/api/auth/updateLike", {
        "UserTelephone": "xiaoqiu-agent", "PostID": post_id,
    }, config)
    if not ok:
        return _finish_failed(config, "toggle_like_post", msg)
    _finish_ok(config, "toggle_like_post", f"post:{post_id}", "toggle like")
    return f"已切换帖子 {post_id} 的点赞状态（原点赞则已取消，未点赞则已点赞）。"


@tool
async def toggle_vote_post(post_id: int, config: RunnableConfig = None) -> str:
    """以当前用户身份给指定帖子投票或取消投票（切换语义，与站内行为一致）。可逆操作。

    Args:
        post_id: 目标帖子的 postID（数字）
    """
    reason = await _precheck(config, "toggle_vote_post")
    if reason:
        return _finish_reject(config, "toggle_vote_post", reason)
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return _finish_reject(config, "toggle_vote_post", "postID 必须是数字")
    if post_id <= 0:
        return _finish_reject(config, "toggle_vote_post", "postID 无效")

    ok, msg = await _go_post("/api/auth/submitVote", {"PostID": post_id}, config)
    if not ok:
        return _finish_failed(config, "toggle_vote_post", msg)
    _finish_ok(config, "toggle_vote_post", f"post:{post_id}", "toggle vote")
    return f"已切换帖子 {post_id} 的投票状态。"


FORUM_WRITE_TOOLS = [create_post, create_comment, toggle_like_post, toggle_vote_post]
