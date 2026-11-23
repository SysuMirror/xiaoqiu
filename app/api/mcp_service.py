"""SSE Market MCP 网关（PAT 鉴权）：把小秋能力按 PAT scope 开放给 MCP 客户端。

协议：MCP 2025-06-18 Streamable HTTP 单点（stateless）模式 —— POST /api/v1/agent/mcp，
支持 initialize / notifications/* / tools/list / tools/call / ping。
鉴权：Authorization: Bearer sk_xq_...（用户 PAT，主站「设置-访问令牌」签发）；
每个请求独立校验（进程内 60s 缓存），身份只来自 token，永不来自请求体（§7 铁律）。
身份流：PAT → Go POST /api/auth/token/verify 换 (userID, scopes)；写侧工具以铸造的
短 JWT（Go 同构 Claims）代表该用户转发 Go API——布隆过滤器/缓存失效/站内通知/排行
联动由 Go 自己保证（与图内 forum 写工具同一转发链，见 tools/forum/write.py 模块注释）。
工具面：tools/list 按 PAT scopes 过滤；tools/call 再次校验 scope（防列表/调用错位）。
限流：per-token 滑动窗口（Limiter，30 req/min）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt as pyjwt
from fastapi import HTTPException, Request

from app import runtime
from app.common.limiter import SlidingWindowLimiter
from app.config import get_config

log = logging.getLogger(__name__)

PROTO_VERSION = "2025-06-18"
_SUPPORTED_VERSIONS = {"2025-03-26", "2025-06-18"}
_SERVER_INFO = {"name": "sse-market-agent", "version": "1.0.0"}
_INSTRUCTIONS = (
    "SSE Market（软工集市）官方 MCP。按令牌 scope 开放：agent:chat=小秋智能体对话；"
    "forum:read=论坛检索（关键词/最新/热门/评论/分区统计/知识库语义检索）；"
    "forum:write=以你的身份发帖/评论。"
)

_GO_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_PAT_CACHE_TTL_S = 60.0
_RATE_LIMIT_PER_MIN = 30
_JWT_TTL_S = 7200

_limiter = SlidingWindowLimiter()
_pat_cache: dict[str, tuple[float, "PatIdentity"]] = {}


@dataclass(frozen=True)
class PatIdentity:
    user_id: int
    scopes: frozenset[str]

    @property
    def uid_str(self) -> str:
        return f"u{self.user_id}"

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token (PAT, sk_xq_...)")
    token = auth[7:].strip()
    if not token.startswith("sk_xq_"):
        raise HTTPException(status_code=401, detail="invalid token format")
    return token


async def _go_base() -> str:
    from app.common.goaddr import resolve_go_base

    cfg = get_config()
    return await resolve_go_base(cfg.go_backend.container_name) or cfg.forum_write.base_url


async def _verify_pat(token: str) -> PatIdentity:
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    hit = _pat_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=_GO_TIMEOUT, trust_env=False) as hc:
            resp = await hc.post(f"{await _go_base()}/api/auth/token/verify",
                                 json={}, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001
        log.warning("mcp pat verify unreachable: %s", e)
        raise HTTPException(status_code=503, detail="token service unavailable") from e
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    body = resp.json() or {}
    data = body.get("data") or {}
    uid, scopes = int(data.get("user_id") or 0), list(data.get("scopes") or [])
    if body.get("code") != 200 or uid <= 0 or not data.get("is_active", True):
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    ident = PatIdentity(user_id=uid, scopes=frozenset(scopes))
    if len(_pat_cache) > 4096:  # 简易防膨胀：过期项自然淘汰，超阈值整体清一次
        _pat_cache.clear()
    _pat_cache[key] = (now + _PAT_CACHE_TTL_S, ident)
    return ident


def _mint_jwt(uid: int) -> str:
    """铸造 Go 同构短 JWT（Claims{UserID, sub:"user token", iss:"loginTest"}，HS256）。"""
    now = int(time.time()) - 5
    return pyjwt.encode(
        {"UserID": uid, "sub": "user token", "iss": "loginTest",
         "iat": now, "exp": now + _JWT_TTL_S},
        os.environ.get("JWT_SECRET", ""), algorithm="HS256")


class _McpError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


def _rpc(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _json_response(payload: dict, status: int = 200):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status, content=payload)


# ---------------- 工具实现 ----------------

def _tool_cfg(ident: PatIdentity, thread_id: str = "") -> dict:
    cfg_ctx: dict = {"user_id": ident.uid_str, "auth_header": f"Bearer {_mint_jwt(ident.user_id)}"}
    if thread_id:
        cfg_ctx["thread_id"] = thread_id
    return {"configurable": cfg_ctx}


async def _t_chat(args: dict, ident: PatIdentity) -> str:
    message = str(args.get("message", "")).strip()
    if not message:
        raise _McpError("message 必填")
    session_id = str(args.get("session_id", "")).strip()[:64]
    ephemeral = not session_id
    if ephemeral:
        session_id = uuid.uuid4().hex
    graph = runtime.get_graph("user")
    if graph is None:
        raise _McpError("智能体启动中，请稍后重试", 50301)
    if not ephemeral:
        from app.memory import threads

        await threads.bind_or_verify(session_id, ident.uid_str, "user")
        await threads.touch(session_id)
    cfg = get_config()
    base = {"configurable": {"thread_id": session_id, "user_id": ident.uid_str,
                             "memory_manager": runtime.state.get("memory_manager"),
                             "auth_header": f"Bearer {_mint_jwt(ident.user_id)}",
                             "run_token_budget": cfg.limits.run_token_budget}}
    config = runtime.trace_patch(base, session_id)
    result = await graph.ainvoke({"messages": [{"role": "user", "content": message}]}, config=config)
    from langchain_core.messages import AIMessage

    reply = ""
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
            reply = m.content if isinstance(m.content, str) else str(m.content)
            break
    if not reply:
        reply = "（小秋没有返回内容，请重试）"
    return f"{reply}\n\n[session_id: {session_id}]" if ephemeral else reply


async def _t_search_posts(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.sql_templates import sql_post_search

    return await sql_post_search.coroutine(
        keyword=str(args.get("keyword", "")), limit=int(args.get("limit", 10)))


async def _t_latest_posts(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.sql_templates import sql_latest_posts

    return await sql_latest_posts.coroutine(limit=int(args.get("limit", 10)))


async def _t_hot_posts(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.read_ext import sql_hot_posts

    return await sql_hot_posts.coroutine(
        limit=int(args.get("limit", 10)), days=int(args.get("days", 7)))


async def _t_post_comments(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.read_ext import sql_post_comments

    return await sql_post_comments.coroutine(
        post_id=int(args.get("post_id", 0)), limit=int(args.get("limit", 10)))


async def _t_board_stats(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.read_ext import sql_board_stats

    return await sql_board_stats.coroutine()


async def _t_kb_search(args: dict, ident: PatIdentity) -> str:
    from app.tools.chat.kb_search import kb_search

    return await kb_search.coroutine(
        query=str(args.get("query", "")), top_k=int(args.get("top_k", 5)))


async def _t_create_post(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.write import create_post

    return await create_post.coroutine(
        title=str(args.get("title", "")), content=str(args.get("content", "")),
        partition=str(args.get("partition", "")), tag=str(args.get("tag", "")),
        is_anonymous=bool(args.get("is_anonymous", False)),
        config=_tool_cfg(ident, f"mcp:{uuid.uuid4().hex[:12]}"))


async def _t_create_comment(args: dict, ident: PatIdentity) -> str:
    from app.tools.forum.write import create_comment

    return await create_comment.coroutine(
        post_id=int(args.get("post_id", 0)), content=str(args.get("content", "")),
        config=_tool_cfg(ident, f"mcp:{uuid.uuid4().hex[:12]}"))


def _schema(*required: str, **props) -> dict:
    return {"type": "object", "required": list(required), "properties": props}


def _s_str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _s_int(desc: str, lo: int, hi: int, default: int) -> dict:
    return {"type": "integer", "description": desc, "minimum": lo, "maximum": hi,
            "default": default}


def _s_bool(desc: str, default: bool) -> dict:
    return {"type": "boolean", "description": desc, "default": default}


# (name, scope, description, inputSchema, handler)
TOOLS: list[tuple[str, str, str, dict, object]] = [
    ("xiaoqiu_chat", "agent:chat",
     "与小秋智能体对话（完整 agent 能力：可自主检索论坛/知识库/联网，受托写操作走人审）。"
     "message 一句话描述需求即可；返回文本末尾附 session_id，继续该对话时原样传入。",
     _schema("message",
             message=_s_str("要对小秋说的话"),
             session_id=_s_str("可选：上次返回的 session_id，用于多轮对话")),
     _t_chat),
    ("forum_search_posts", "forum:read",
     "按关键词搜索论坛帖子（标题/内容精确含该词），返回 postID/标题/作者/时间/浏览数。"
     "语义/模糊检索请用 forum_kb_search。",
     _schema("keyword",
             keyword=_s_str("搜索关键词（1~60 字符，不支持通配符）"),
             limit=_s_int("返回条数", 1, 20, 10)),
     _t_search_posts),
    ("forum_latest_posts", "forum:read",
     "获取论坛最新发表的帖子列表（postID/标题/作者/时间/浏览数）。",
     _schema(limit=_s_int("返回条数", 1, 20, 10)),
     _t_latest_posts),
    ("forum_hot_posts", "forum:read",
     "获取近期热门帖子（按 heat 热度排序，默认最近 7 天）。",
     _schema(limit=_s_int("返回条数", 1, 20, 10),
             days=_s_int("时间窗口天数", 1, 30, 7)),
     _t_hot_posts),
    ("forum_post_comments", "forum:read",
     "查看指定帖子的评论列表（内容/时间/点赞数，作者脱敏别名）。post_id 可从"
     " forum_search_posts / forum_latest_posts 等结果获得。",
     _schema("post_id",
             post_id=_s_int("帖子 postID", 1, 2**31 - 1, 1),
             limit=_s_int("返回条数", 1, 30, 10)),
     _t_post_comments),
    ("forum_board_stats", "forum:read",
     "查看论坛各分区的帖子数量、总浏览量与平均热度（板块概览）。",
     _schema(),
     _t_board_stats),
    ("forum_kb_search", "forum:read",
     "论坛知识库语义检索（向量检索，关键词未必原样出现）。返回最相关的帖子切片与链接。",
     _schema("query",
             query=_s_str("用完整中文句子描述要找的内容"),
             top_k=_s_int("返回条数", 1, 5, 5)),
     _t_kb_search),
    ("forum_create_post", "forum:write",
     "以令牌主人身份在论坛发布新帖（调用即发布，会经过内容安全审核与每日限频）。"
     "分区只能是：生活日常/主页/学习交流/技术分享/课程专区/实习就业/社团活动/宿舍生活/组团捞人/期末资料/其他。",
     _schema("title", "content", "partition",
             title=_s_str("帖子标题，最多 30 字"),
             content=_s_str("帖子正文"),
             partition=_s_str("分区（见描述中的枚举）"),
             tag=_s_str("可选：标签，单个词"),
             is_anonymous=_s_bool("是否匿名发布", False)),
     _t_create_post),
    ("forum_create_comment", "forum:write",
     "以令牌主人身份在指定帖子下发布一条评论（最多 1000 字，调用即发布，"
     "经过内容安全审核与每日限频）。",
     _schema("post_id", "content",
             post_id=_s_int("目标帖子 postID", 1, 2**31 - 1, 1),
             content=_s_str("评论文本")),
     _t_create_comment),
]


def _tools_for(ident: PatIdentity) -> list[dict]:
    return [{"name": name, "description": desc, "inputSchema": schema}
            for name, scope, desc, schema, _h in TOOLS if ident.has(scope)]


def _tool_entry(name: str, ident: PatIdentity) -> tuple[str, object] | None:
    for t_name, scope, _d, _s, handler in TOOLS:
        if t_name == name:
            if not ident.has(scope):
                return None
            return scope, handler
    return None


# ---------------- 协议入口 ----------------

async def _on_initialize(params: dict, ident: PatIdentity) -> dict:
    requested = str((params or {}).get("protocolVersion", "") or "")
    return {
        "protocolVersion": requested if requested in _SUPPORTED_VERSIONS else PROTO_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": dict(_SERVER_INFO),
        "instructions": _INSTRUCTIONS,
    }


async def _on_tools_list(params: dict, ident: PatIdentity) -> dict:
    return {"tools": _tools_for(ident)}


async def _on_tools_call(params: dict, ident: PatIdentity) -> dict:
    name = str((params or {}).get("name", ""))
    args = (params or {}).get("arguments") or {}
    entry = _tool_entry(name, ident)
    if entry is None:
        raise _McpError(f"未知工具或令牌 scope 未授权：{name}", -32602)
    _scope, handler = entry
    try:
        text = await handler(args, ident)
    except _McpError:
        raise
    except HTTPException as e:
        return {"content": [{"type": "text", "text": f"工具执行失败：{e.detail}"}],
                "isError": True}
    except Exception as e:  # noqa: BLE001 —— 工具内部错误按 MCP 约定回 isError，不 500
        log.exception("mcp tools/call %s failed", name)
        return {"content": [{"type": "text", "text": f"工具执行异常：{e}"}],
                "isError": True}
    if not isinstance(text, str):
        text = str(text)
    return {"content": [{"type": "text", "text": text}]}


async def _on_ping(params: dict, ident: PatIdentity) -> dict:
    return {}


_METHODS = {
    "initialize": _on_initialize,
    "tools/list": _on_tools_list,
    "tools/call": _on_tools_call,
    "ping": _on_ping,
}


async def mcp_endpoint(request: Request):
    """MCP 单点入口（stateless）：鉴权 → 限流 → JSON-RPC 分发。"""
    token = _bearer(request)
    ok, retry = _limiter.allow(f"mcp:{hashlib.sha256(token.encode()).hexdigest()}",
                               _RATE_LIMIT_PER_MIN)
    if not ok:
        raise HTTPException(status_code=429, detail=f"rate limited, retry in {retry}s")
    ident = await _verify_pat(token)

    try:
        msg = await request.json()
    except Exception:  # noqa: BLE001
        return _json_response(_rpc_error(None, -32700, "parse error"), 400)
    if not isinstance(msg, dict):
        return _json_response(_rpc_error(None, -32600, "invalid request"), 400)

    method = str(msg.get("method", ""))
    if method.startswith("notifications/"):
        from fastapi.responses import Response

        return Response(status_code=202)
    handler = _METHODS.get(method)
    id_ = msg.get("id")
    if handler is None:
        return _json_response(_rpc_error(id_, -32601, f"method not found: {method}"))
    try:
        result = await handler(msg.get("params") or {}, ident)
    except _McpError as e:
        return _json_response(_rpc_error(id_, e.code, str(e)))
    return _json_response(_rpc(id_, result))
