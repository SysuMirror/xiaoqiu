"""客户端生产化 API（对标豆包）：会话中心 / 记忆管理 / 消息反馈 / 开场建议。

鉴权同 compat：JWT Bearer；会话与记忆全部按 user_id 隔离。
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessage, HumanMessage

from app import runtime
from app.api import deps
from app.memory import feedback, threads


def _ok(data=None) -> dict:
    return {"code": 0, "msg": "ok", "data": data or {}}


def _err(status: int, code: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "msg": msg})


async def _identity(request: Request) -> deps.UserIdentity:
    return deps.verify_request(request)


async def _require_owner(session_id: str, user_id: str) -> None:
    owner = await threads.owner_of(session_id)
    if not owner:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")


# ---------- 会话中心 ----------

async def conversations_list(request: Request):
    ident = await _identity(request)
    limit = int(request.query_params.get("limit", "50") or 50)
    offset = int(request.query_params.get("offset", "0") or 0)
    items = await threads.list_threads(ident.user_id, limit=limit, offset=offset)
    return _ok({"conversations": items})


async def conversation_messages(request: Request):
    ident = await _identity(request)
    session_id = request.path_params["session_id"][:64]
    await _require_owner(session_id, ident.user_id)
    graph = runtime.get_graph(ident.role)
    if graph is None:
        return _err(503, 50301, "服务启动中")
    messages = []
    try:
        snap = await graph.aget_state({"configurable": {"thread_id": session_id}})
        for m in (snap.values or {}).get("messages", []):
            if isinstance(m, HumanMessage) and m.content:
                messages.append({"role": "user", "content": str(m.content)})
            elif isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                messages.append({"role": "assistant", "content": str(m.content)})
        messages = messages[-200:]
    except Exception:  # noqa: BLE001 —— 历史读取失败返回空列表不阻塞
        pass
    title = await threads.get_title(session_id)
    return _ok({"session_id": session_id, "title": title, "messages": messages})


async def conversation_rename(request: Request):
    ident = await _identity(request)
    session_id = request.path_params["session_id"][:64]
    await _require_owner(session_id, ident.user_id)
    body = await request.json()
    title = str(body.get("title", "")).strip()
    if not title:
        return _err(422, 40001, "title 必填")
    ok = await threads.set_title(session_id, ident.user_id, title)
    return _ok({"updated": ok, "title": title.strip()[:60]})


async def conversation_delete(request: Request):
    ident = await _identity(request)
    session_id = request.path_params["session_id"][:64]
    await _require_owner(session_id, ident.user_id)
    ok = await threads.soft_delete(session_id, ident.user_id)
    return _ok({"deleted": ok})


# ---------- 记忆管理 ----------

def _manager():
    mgr = runtime.memory_manager()
    if mgr is None:
        raise HTTPException(status_code=503, detail="记忆系统未就绪")
    return mgr


async def memory_list(request: Request):
    ident = await _identity(request)
    mgr = _manager()
    items = await mgr.list_all(ident.user_id)
    return _ok({"memories": [{
        "key": it["key"],
        "text": it["text"],
        "updated_at": it["updated_at"].isoformat(),
    } for it in reversed(items)]})  # 最新在前


async def memory_delete(request: Request):
    ident = await _identity(request)
    mgr = _manager()
    ok = await mgr.delete(ident.user_id, request.path_params["key"])
    if not ok:
        return _err(404, 40401, "记忆不存在")
    return _ok({"deleted": True})


async def memory_update(request: Request):
    ident = await _identity(request)
    mgr = _manager()
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return _err(422, 40001, "text 必填")
    ok = await mgr.update(ident.user_id, request.path_params["key"], text)
    if not ok:
        return _err(404, 40401, "记忆不存在")
    return _ok({"updated": True})


# ---------- 消息反馈 ----------

async def message_feedback(request: Request):
    ident = await _identity(request)
    body = await request.json()
    run_id = str(body.get("run_id", "")).strip()[:64]
    thread_id = str(body.get("thread_id", "")).strip()[:64]
    try:
        rating = int(body.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0
    if not run_id or rating == 0:
        return _err(422, 40001, "run_id 与 rating(1/-1) 必填")
    action = await feedback.submit(run_id, thread_id, ident.user_id, rating,
                                   str(body.get("comment", "")))
    return _ok({"result": action})


async def feedback_stats(request: Request):
    deps.verify_request(request)
    days = int(request.query_params.get("days", "30") or 30)
    return _ok(await feedback.stats(days=min(max(days, 1), 365)))


# ---------- 开场建议 ----------

_SEED_SUGGESTIONS = [
    "帮我看看论坛最新有哪些帖子",
    "知识库里有没有写实验报告或毕设的经验帖",
    "我想反馈一个 bug 或者提个建议",
]


async def suggestions(request: Request):
    deps.verify_request(request)
    items: list[dict] = []
    try:
        from app.tools.forum import sql_templates

        for p in await sql_templates.latest_posts_raw(3):
            items.append({"text": f"帖子《{p['title']}》讲了什么？", "post_id": p["post_id"]})
    except Exception:  # noqa: BLE001 —— 论坛库不可用时只给固定建议
        pass
    texts = [i["text"] for i in items]
    for s in _SEED_SUGGESTIONS:
        if s not in texts:
            items.append({"text": s, "post_id": ""})
    return _ok({"suggestions": items[:6]})


# ---------- 用量 / 导出 / 删除权（v3 成本归因 + 数据生命周期） ----------

async def me_usage(request: Request):
    ident = await _identity(request)
    from app.memory import runs

    days = int(request.query_params.get("days", "30") or 30)
    return _ok({"usage": await runs.usage_by_day(ident.user_id, days=days),
                "totals": await runs.user_totals(ident.user_id)})


async def me_export(request: Request):
    """导出用户全部小秋数据（会话/记忆/反馈/用量），JSON 下载。"""
    ident = await _identity(request)
    graph = runtime.get_graph(ident.role)
    from app.memory import runs as _runs

    conversations = []
    for t in (await threads.list_threads(ident.user_id, limit=100))[:100]:
        msgs: list[dict] = []
        if graph is not None:
            try:
                snap = await graph.aget_state({"configurable": {"thread_id": t["session_id"]}})
                for m in (snap.values or {}).get("messages", []):
                    if isinstance(m, HumanMessage) and m.content:
                        msgs.append({"role": "user", "content": str(m.content)})
                    elif isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                        msgs.append({"role": "assistant", "content": str(m.content)})
                msgs = msgs[-200:]
            except Exception:  # noqa: BLE001
                pass
        conversations.append({**t, "messages": msgs})
    memories = []
    try:
        mgr = runtime.memory_manager()
        if mgr is not None:
            memories = [{"text": it["text"], "updated_at": it["updated_at"].isoformat()}
                        for it in await mgr.list_all(ident.user_id)]
    except Exception:  # noqa: BLE001
        pass
    payload = {
        "exported_at": __import__("datetime").datetime.now().isoformat(),
        "user_id": ident.user_id,
        "conversations": conversations,
        "memories": memories,
        "feedback": await feedback.list_for_user(ident.user_id),
        "usage_totals": await _runs.user_totals(ident.user_id),
    }
    from fastapi.responses import JSONResponse as JR

    return JR(content=payload, headers={
        "Content-Disposition": f'attachment; filename="xiaoqiu-export-{ident.user_id}.json"'})


_delete_tokens: dict[str, tuple[str, float]] = {}  # user_id -> (token, expire monotonic)
_DELETE_TTL = 600.0


async def me_delete(request: Request):
    """删除用户全部小秋数据（两步确认）。台账按审计要求保留但剥离身份与内容。"""
    ident = await _identity(request)
    import time as _t
    import uuid as _uuid

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    confirm = str(body.get("confirm", ""))
    entry = _delete_tokens.get(ident.user_id)
    if not confirm or entry is None or entry[0] != confirm or _t.monotonic() > entry[1]:
        token = _uuid.uuid4().hex
        _delete_tokens[ident.user_id] = (token, _t.monotonic() + _DELETE_TTL)
        return _ok({"confirm_required": True, "confirm_token": token,
                    "expires_in_seconds": int(_DELETE_TTL),
                    "msg": "此操作不可恢复：将清除全部会话/记忆/反馈/运行记录。"
                           "用返回的 confirm_token 再次调用以确认删除"})
    _delete_tokens.pop(ident.user_id, None)

    from app import runtime as _rt
    from app.memory import ledger
    from app.memory import runs as _runs

    purged = {"threads": 0, "memories": 0, "feedback": 0, "runs": 0, "actions_anonymized": 0}
    thread_ids = await threads.all_thread_ids(ident.user_id)
    await threads.purge_threads(thread_ids)
    purged["threads"] = len(thread_ids)
    try:
        mgr = _rt.memory_manager()
        if mgr is not None:
            purged["memories"] = await mgr.delete_all(ident.user_id)
    except Exception:  # noqa: BLE001 —— 向量镜像失败不阻塞事实源清理
        pass
    purged["feedback"] = await feedback.delete_user(ident.user_id)
    purged["runs"] = await _runs.delete_user(ident.user_id)
    purged["actions_anonymized"] = await ledger.anonymize_actor(ident.user_id)
    await ledger.record("system", "user_data_delete", "success", target=ident.user_id,
                        detail=str(purged))
    return _ok({"deleted": True, **purged})


async def conversation_export(request: Request):
    """导出单个会话为 Markdown 文本下载。"""
    ident = await _identity(request)
    session_id = request.path_params["session_id"][:64]
    await _require_owner(session_id, ident.user_id)
    graph = runtime.get_graph(ident.role)
    if graph is None:
        return _err(503, 50301, "服务启动中")
    lines = [f"# 小秋会话导出：{await threads.get_title(session_id) or session_id}", ""]
    try:
        snap = await graph.aget_state({"configurable": {"thread_id": session_id}})
        for m in (snap.values or {}).get("messages", []):
            if isinstance(m, HumanMessage) and m.content:
                lines += [f"**用户**：{m.content}", ""]
            elif isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                lines += [f"**小秋**：{m.content}", ""]
    except Exception:  # noqa: BLE001
        pass
    from fastapi.responses import Response as HTTPResponse

    return HTTPResponse(content="\n".join(lines), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="xiaoqiu-{session_id[:12]}.md"'})


async def feedback_admin_list(request: Request):
    """反馈工单列表（admin 专用）。"""
    ident = await _identity(request)
    if ident.role != "admin":
        return _err(403, 40301, "需要 admin 角色")
    limit = int(request.query_params.get("limit", "100") or 100)
    return _ok({"items": await feedback.list_recent(limit=limit),
                "stats": await feedback.stats(days=30)})
