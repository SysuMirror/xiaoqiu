"""实时 RAG 维护：webhook（发帖即录/删即除/转私密即除）+ 定时对账。

端点（仅内网，nginx 不路由 /internal/，且要求 X-Internal-Token）：
  POST /internal/rag/upsert  {type: post|comment, id}
  POST /internal/rag/delete  {type: post|comment, id}   # post 级联删其评论
对账（每 10 分钟）：索引里的 doc 若在 MySQL 中已不可见（删除/转私密）→ 补删；
  MySQL 中有而索引缺的新帖 → 补录（webhook 漏发兜底）。
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Request

from app.common import skein_signature
from app.rag import store
from app.tools.forum.sql_templates import _query, init_forum_pool

log = logging.getLogger(__name__)
router = APIRouter()


def _check_token(request: Request) -> bool:
    expected = os.environ.get("INTERNAL_TOKEN", "")
    if not expected:
        return False
    return request.headers.get("X-Internal-Token", "") == expected


def _check_skein(request: Request, body: bytes) -> bool:
    """雁阵的状态回调：验证 X-Skein-Webhook-Signature（密钥 SKEIN_WEBHOOK_SECRET）。

    这样小秋不需要持有雁阵的任何令牌——回调的信任来自双方共享的一把签名密钥。
    """
    secret = os.environ.get("SKEIN_WEBHOOK_SECRET", "")
    return skein_signature.verify(
        body,
        request.headers.get("X-Skein-Webhook-Timestamp"),
        request.headers.get("X-Skein-Webhook-Signature"),
        secret,
    )


def _forbidden():
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=403, content={"error": "forbidden"})


async def _fetch_post(post_id: int) -> dict | None:
    await init_forum_pool()
    _, rows = await _query(
        "/*T*/ SELECT postID, title, ptext, post_time FROM posts p"
        " WHERE p.postID=%s AND p.is_private=0",
        (post_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return {"id": f"post:{r[0]}", "source": "forum_post",
            "title": str(r[1] or ""), "content": str(r[2] or ""), "post_time": r[3]}


async def _fetch_comment(comment_id: int) -> dict | None:
    await init_forum_pool()
    _, rows = await _query(
        "/*T*/ SELECT c.pcommentID, c.pctext, c.time, c.ptargetID FROM pcomments c"
        " JOIN posts p ON p.postID=c.ptargetID"
        " WHERE c.pcommentID=%s AND p.is_private=0",
        (comment_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return {"id": f"comment:{r[0]}", "source": "forum_comment",
            "title": f"评论于帖子{r[3]}", "content": str(r[1] or ""), "post_time": r[2],
            "parent_post_id": f"post:{r[3]}"}


@router.post("/internal/skein/notify")
async def skein_notify(request: Request):
    """Skein 工单状态 → 小秋在前厅原帖回写公开进度。

    请求体是雁阵的 ``ticket.status`` 事件：
    ``{"event": "ticket.status", "ticket": {code, public_status, source, external_id, …}}``
    """
    raw = await request.body()
    if not _check_skein(request, raw):
        return _forbidden()
    from app.frontdesk import post_reply, public_message

    try:
        body = json.loads(raw)
    except ValueError:
        return {"ok": True, "skipped": True}
    ticket = body.get("ticket") if isinstance(body, dict) else None
    ticket = ticket if isinstance(ticket, dict) else {}
    origin = str(ticket.get("source") or "")
    origin_id = str(ticket.get("external_id") or "")
    reply_ccomment_id = 0
    if origin == "forum_post":
        try:
            post_id, pcomment_id = int(origin_id), 0
        except ValueError:
            return {"ok": True, "skipped": True}
    elif origin == "forum_comment":
        try:
            post_id_s, pcomment_id_s = origin_id.split(":", 1)
            post_id, pcomment_id = int(post_id_s), int(pcomment_id_s)
        except (ValueError, TypeError):
            return {"ok": True, "skipped": True}
    elif origin == "forum_ccomment":
        try:
            post_id_s, pcomment_id_s, ccomment_id_s = origin_id.split(":", 2)
            post_id, pcomment_id = int(post_id_s), int(pcomment_id_s)
            reply_ccomment_id = int(ccomment_id_s)
        except (ValueError, TypeError):
            return {"ok": True, "skipped": True}
    else:
        return {"ok": True, "skipped": True}
    text = public_message(
        str(ticket.get("public_status") or ""), str(ticket.get("code") or "")
    )
    if not text:
        return {"ok": True, "skipped": True}
    ok = await post_reply(post_id, pcomment_id, text, reply_ccomment_id)
    return {"ok": ok}


@router.post("/internal/rag/upsert")
async def rag_upsert(request: Request):
    if not _check_token(request):
        return _forbidden()
    body = await request.json()
    doc_type, doc_id = body.get("type"), int(body.get("id", 0))
    fetch = _fetch_post if doc_type == "post" else _fetch_comment
    doc = await fetch(doc_id)
    if doc is None:
        # 不存在或已私密 → 视为删除语义
        await store.delete_points([f"{doc_type}:{doc_id}"])
        if doc_type == "post":
            await store.delete_by_parent(f"post:{doc_id}")
        return {"ok": True, "action": "deleted"}
    await store.upsert_documents([doc])
    return {"ok": True, "action": "upserted"}


@router.post("/internal/rag/delete")
async def rag_delete(request: Request):
    if not _check_token(request):
        return _forbidden()
    body = await request.json()
    doc_type, doc_id = body.get("type"), int(body.get("id", 0))
    doc_ids = [f"{doc_type}:{doc_id}"]
    if doc_type == "post":
        await store.delete_by_parent(f"post:{doc_id}")  # 级联删评论点
    await store.delete_points(doc_ids)
    return {"ok": True, "action": "deleted"}


@router.post("/internal/rag/reconcile")
async def rag_reconcile(request: Request):
    if not _check_token(request):
        return _forbidden()
    return await reconcile()


async def reconcile() -> dict:
    """对账：索引有但 MySQL 不可见 → 删；近 24h 新帖缺索引 → 补录。"""
    post_ids, comment_ids = [], []
    async for doc_id, source, _parent in store.iter_doc_ids():
        if source == "forum_post" and ":" in doc_id:
            post_ids.append(int(doc_id.split(":")[1]))
        elif source == "forum_comment" and ":" in doc_id:
            comment_ids.append(int(doc_id.split(":")[1]))

    await init_forum_pool()
    stale: list[str] = []
    for ids, table, key in ((post_ids, "posts", "postID"), (comment_ids, "pcomments", "pcommentID")):
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            ph = ",".join(["%s"] * len(batch))
            if table == "posts":
                _, rows = await _query(
                    f"/*T*/ SELECT postID FROM posts WHERE postID IN ({ph}) AND is_private=0",
                    tuple(batch))
            else:
                _, rows = await _query(
                    f"/*T*/ SELECT c.pcommentID FROM pcomments c JOIN posts p"
                    f" ON p.postID=c.ptargetID WHERE c.pcommentID IN ({ph}) AND p.is_private=0",
                    tuple(batch))
            alive = {r[0] for r in rows}
            stale += [f"{key == 'postID' and 'post' or 'comment'}:{x}" for x in batch if x not in alive]
    if stale:
        await store.delete_points(stale)

    # 补录：最近 24h 公开帖中缺索引的
    _, rows = await _query(
        "/*T*/ SELECT p.postID, p.title, p.ptext, p.post_time FROM posts p"
        " WHERE p.is_private=0 AND p.post_time >= NOW() - INTERVAL 1 DAY"
        " ORDER BY p.post_time DESC LIMIT 200",
        ())
    indexed = set(post_ids)
    missing = [
        {"id": f"post:{r[0]}", "source": "forum_post", "title": str(r[1] or ""),
         "content": str(r[2] or ""), "post_time": r[3]}
        for r in rows if r[0] not in indexed
    ]
    # 补录：最近 24h 公开帖的评论中缺索引的（webhook 漏发兜底）
    indexed_c = set(comment_ids)
    _, crows = await _query(
        "/*T*/ SELECT c.pcommentID, c.pctext, c.time, c.ptargetID FROM pcomments c"
        " JOIN posts p ON p.postID=c.ptargetID"
        " WHERE p.is_private=0 AND c.time >= NOW() - INTERVAL 1 DAY"
        " ORDER BY c.time DESC LIMIT 300",
        ())
    missing += [
        {"id": f"comment:{r[0]}", "source": "forum_comment", "title": f"评论于帖子{r[3]}",
         "content": str(r[1] or ""), "post_time": r[2], "parent_post_id": f"post:{r[3]}"}
        for r in crows if r[0] not in indexed_c
    ]
    if missing:
        await store.upsert_documents(missing)
    return {"removed": len(stale), "reindexed": len(missing)}
