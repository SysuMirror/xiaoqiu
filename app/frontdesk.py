"""小秋前厅：轮询论坛吐槽/反馈，回执、建单、报进度。

只做前台：判断是否要回、怎么写、是否建单；工单与派工全部交给 Skein。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx

from app import db

log = logging.getLogger("sse-agent.frontdesk")

SYSTEM = """你是小秋，集市论坛的助手账号。根据一条论坛内容判断并写回复。
- action: reply（所有吐槽/反馈都回）或 skip（广告/灌水/无意义/纯提问）。
- kind: bug / request / venting / skip。
- 纯吐槽/情绪：共情、轻松，不承诺时间；reply 1-3 句。
- 可执行 bug/需求：专业、简洁；reply 说“收到，已登记，我先复现一下”；不要编造修复。
- 不嘲讽、不站队、不泄露内部信息；回复不超过 200 字。
只输出 JSON：
{"action":"reply|skip","kind":"bug|request|venting|skip","title":"...","summary":"...","severity":"low|normal|high","reply":"..."}"""

COMPLAINT = re.compile(
    r"bug|报错|错误|坏了|不行|打不开|闪退|崩溃|卡顿|卡死|很慢|太慢|"
    r"上传|下载|登录|注册|图片|视频|头像|评论|建议|希望|能不能|"
    r"吐槽|无语|难用|体验|问题|求助|异常|失败|失联",
    re.IGNORECASE,
)
ACTIONABLE = re.compile(
    r"bug|报错|错误|坏|不行|打不开|闪退|崩|上传|下载|登录|注册|卡|失败|异常",
    re.IGNORECASE,
)

DDL = """
CREATE TABLE IF NOT EXISTS frontdesk_seen (
    kind       TEXT NOT NULL,
    item_id    BIGINT NOT NULL,
    status     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, item_id)
);
"""


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _plain(text: Any, limit: int = 2000) -> str:
    raw = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", raw).strip()[:limit]


def _parse_json(text: str) -> dict | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def public_message(status: str, code: str) -> str:
    suffix = f"（{code}）" if code else ""
    if status == "working":
        return f"小秋更新：已收到，处理中{suffix}。有补充可以直接回我。"
    if status == "fixed":
        return f"小秋更新：已修复，维护者已验收{suffix}。你试一下，没好直接回我。"
    if status == "need_info":
        return f"小秋更新：这条还需要补充复现步骤{suffix}。"
    return ""


def _skein_base() -> str:
    return os.environ.get("SKEIN_CONTROL_URL", "http://172.17.0.1:7800").rstrip("/")


def _skein_key() -> str:
    # 助手凭证：只能建单、查自己建的单、上报留痕；拿不到主令牌的派单/审批权限。
    return os.environ.get("SKEIN_ASSISTANT_KEY", "")


async def _skein_post(
    path: str, payload: dict, timeout: float = 10.0
) -> tuple[int, dict | None]:
    """POST 到雁阵控制面的公共 API（``/api/v1``），用助手凭证认证。

    返回 (状态码, JSON 体)。网络失败记 (0, None)；没配助手凭证直接 (0, None)。
    """
    key = _skein_key()
    if not key:
        return 0, None
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{_skein_base()}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        log.warning("skein %s failed: %s", path, exc)
        return 0, None
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code != 200 and response.status_code != 201:
        # 公共 API 的错误体是 {"detail": {"code", "message"}}；只记它，不记请求头（里面是凭据）
        detail = data.get("detail") if isinstance(data, dict) else None
        log.warning("skein %s -> %s %s", path, response.status_code, detail)
    return response.status_code, data if isinstance(data, dict) else None


async def _skein_submit(payload: dict) -> dict | None:
    """建单（``POST /api/v1/tickets``）。同一 (source, external_id) 重报是幂等的。"""
    status, data = await _skein_post("/api/v1/tickets", payload)
    if status in (200, 201):
        return data
    return None


async def _skein_tickets() -> list[dict]:
    """这个助手提交过的工单（最近的 200 张），用来避免同一帖重复建单。"""
    key = _skein_key()
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                f"{_skein_base()}/api/v1/tickets?limit=200",
                headers={"Authorization": f"Bearer {key}"},
            )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("skein list tickets failed: %s", exc)
        return []
    tickets = data.get("tickets") if isinstance(data, dict) else None
    return [ticket for ticket in tickets or [] if isinstance(ticket, dict)]


def _open_threads(tickets: list[dict]) -> dict[str, str]:
    """在途工单按帖分组：external_id 的第一段是帖子 id，值是工单编号。

    公共 API 的一个 ``(source, external_id)`` 是一次请求；同一帖的后续评论是独立请求，
    所以在小秋这边按帖去重：一帖已在处理，后续补充不再建新单（原帖里也看得到）。
    """
    found: dict[str, str] = {}
    for ticket in tickets:
        head = str(ticket.get("external_id") or "").split(":", 1)[0]
        if not head.isdigit():
            continue
        if str(ticket.get("status") or "") in ("done", "rejected", "cancelled"):
            continue
        if str(ticket.get("code") or ""):
            found.setdefault(head, str(ticket["code"]))
    return found


async def _skein_trace(payload: dict) -> None:
    """模型调用留痕（``POST /api/v1/llm/calls``，需部署方开启 SKEIN_API_TRACE）。"""
    await _skein_post("/api/v1/llm/calls", payload, timeout=3.0)


def _ticket_payload(item: dict, verdict: dict) -> dict:
    """一条论坛内容 → 雁阵的建单请求。

    ``source`` 记来源形态（帖子/评论/楼中楼），``external_id`` 是它在论坛里的坐标——
    两者合起来是幂等键，也是状态回调用来找回原帖的依据。
    """
    kind = item["kind"]
    post_id = str(item["post_id"])
    if kind == "post":
        external_id = post_id
    elif kind == "comment":
        external_id = f"{post_id}:{item['id']}"
    else:
        external_id = f"{post_id}:{item['pcomment_id']}:{item['id']}"
    return {
        "title": _plain(verdict.get("title") or item["title"], 120),
        "description": _plain(verdict.get("summary") or item["text"], 1000),
        "source": {
            "post": "forum_post",
            "comment": "forum_comment",
            "ccomment": "forum_ccomment",
        }[kind],
        "external_id": external_id,
        "requester": f"forum:{item['user_id']}",
        "category": str(verdict.get("kind") or "bug"),
        "severity": str(verdict.get("severity") or "normal"),
        "labels": ["frontdesk"],
    }


def _ticket_code(ticket: dict | None) -> str:
    ticket = (ticket or {}).get("ticket") or {}
    return str(ticket.get("code") or "")


async def _trace(
    session_id: str,
    model: str,
    prompt: str,
    response: str,
    usage: dict,
    latency_ms: int,
    status: str = "ok",
    error: str = "",
) -> None:
    # 谁产生的调用由雁阵按凭证认定，这里只报调用本身
    await _skein_trace(
        {
            "session_id": session_id,
            "source": "frontdesk",
            "model": model,
            "prompt": prompt[:16000],
            "response": response[:16000],
            "prompt_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "completion_tokens": int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            ),
            "latency_ms": latency_ms,
            "status": status,
            "error": error[:2000],
        }
    )


def _fallback(item: dict, mention: bool = False) -> dict:
    text = item["text"]
    title = _plain(item["title"] or text, 48)
    if ACTIONABLE.search(text):
        return {
            "action": "reply",
            "kind": "bug",
            "title": title,
            "summary": _plain(text, 500),
            "severity": "normal",
            "reply": "收到，已登记，我先复现一下；有复现步骤或截图可以回我。",
        }
    if mention:
        return {
            "action": "reply",
            "kind": "venting",
            "title": title,
            "summary": _plain(text, 500),
            "severity": "low",
            "reply": "看到了，具体说说？有复现步骤或截图可以贴给我。",
        }
    return {"action": "skip", "kind": "skip"}


async def classify(item: dict, mention: bool = False) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    base = os.environ.get("FRONTDESK_LLM_BASE", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("FRONTDESK_MODEL", "deepseek-v4-flash-vision-exp")
    if not key:
        return _fallback(item, mention)
    user = f"位置：{item['position']}\n内容：{item['text']}"
    if mention:
        user += "\n（该条 @ 了小秋）"
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            response = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
            )
        response.raise_for_status()
        data = response.json()
        content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = _parse_json(content)
        if parsed is None:
            raise ValueError("model returned non-json")
        await _trace(
            f"forum:{item['kind']}:{item['id']}",
            model,
            user,
            content,
            data.get("usage") or {},
            int((time.monotonic() - started) * 1000),
        )
        return parsed
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        await _trace(
            f"forum:{item['kind']}:{item['id']}",
            model,
            user,
            "",
            {},
            int((time.monotonic() - started) * 1000),
            status="error",
            error=str(exc),
        )
        log.warning("frontdesk classify failed: %s", exc)
        return _fallback(item, mention)


async def post_reply(
    post_id: int, pcomment_id: int, content: str, reply_ccomment_id: int = 0
) -> bool:
    base = os.environ.get("GO_INTERNAL_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.environ.get("INTERNAL_TOKEN", "")
    if not token or not content.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                f"{base}/internal/xiaoqiu/reply",
                json={
                    "post_id": post_id,
                    "pcomment_id": pcomment_id,
                    "reply_ccomment_id": reply_ccomment_id,
                    "content": content[:2800],
                },
                headers={"X-Internal-Token": token},
            )
        return response.status_code == 200
    except httpx.HTTPError as exc:
        log.warning("frontdesk reply failed: %s", exc)
        return False


async def _seen(pool: Any, kind: str, item_id: int) -> str | None:
    return await pool.fetchval(
        "SELECT status FROM frontdesk_seen WHERE kind=$1 AND item_id=$2", kind, item_id
    )


async def _mark(pool: Any, kind: str, item_id: int, status: str) -> None:
    await pool.execute(
        "INSERT INTO frontdesk_seen (kind, item_id, status) VALUES ($1,$2,$3)"
        " ON CONFLICT (kind, item_id) DO UPDATE SET status=EXCLUDED.status,"
        " created_at=now()",
        kind,
        item_id,
        status,
    )


async def _replied_last_hour(pool: Any) -> int:
    return int(
        await pool.fetchval(
            "SELECT count(*) FROM frontdesk_seen"
            " WHERE status='replied' AND created_at > now() - interval '1 hour'"
        )
        or 0
    )


def _candidate(kind: str, text: str) -> bool:
    # 只分诊两类：明确 @小秋（答疑）；命中吐槽词的疑似 bug。
    # 主动回复仅限明确 bug，由 classify 二次确认，避免普通评论打扰用户。
    return bool(text) and len(text) >= 4 and (
        "@小秋" in text or bool(COMPLAINT.search(text))
    )




async def handle(pool: Any, item: dict, open_threads: dict[str, str] | None = None) -> None:
    kind, item_id = item["kind"], int(item["id"])
    status = await _seen(pool, kind, item_id)
    if status in ("replied", "skipped"):
        return
    if not _candidate(kind, item["text"]):
        await _mark(pool, kind, item_id, "skipped")
        return
    if item["user_id"] == _env_int("XQ_BOT_UID", 0):
        await _mark(pool, kind, item_id, "skipped")
        return
    if await _replied_last_hour(pool) >= _env_int("FRONTDESK_MAX_PER_HOUR", 12):
        return
    mention = "@小秋" in item["text"]
    verdict = await classify(item, mention)
    action = str(verdict.get("action") or "")
    if action != "reply":
        await _mark(pool, kind, item_id, "skipped")
        return
    reply = _plain(verdict.get("reply") or "", 600)
    if not reply:
        await _mark(pool, kind, item_id, "skipped")
        return
    kind_of = str(verdict.get("kind") or "venting")
    # 主动回复只允许 bug；非 @ 的建议/吐槽一律不回（bug/request 照常建工单）。
    if kind_of != "bug" and not mention:
        await _mark(pool, kind, item_id, "skipped")
        return
    if kind_of in ("bug", "request"):
        code = ""
        if kind != "post" and open_threads is not None:
            code = open_threads.get(str(item["post_id"]), "")
        if not code:
            ticket = await _skein_submit(_ticket_payload(item, verdict))
            code = _ticket_code(ticket)
            if code and kind != "post" and open_threads is not None:
                open_threads[str(item["post_id"])] = code
        if code and code not in reply:
            reply = reply.rstrip() + f"（{code}）"
    parent_comment = int(item["pcomment_id"]) if kind in ("comment", "ccomment") else 0
    reply_ccomment = int(item_id) if kind == "ccomment" else 0
    ok = await post_reply(
        int(item["post_id"]), parent_comment, reply, reply_ccomment
    )
    await _mark(pool, kind, item_id, "replied" if ok else "failed")
    if ok:
        log.info("frontdesk replied %s:%s kind=%s", kind, item_id, kind_of)


async def one_round(pool: Any) -> None:
    from app.tools.forum.sql_templates import _query, init_forum_pool

    lookback = _env_int("FRONTDESK_LOOKBACK", 600)
    await init_forum_pool()
    _, posts = await _query(
        "/*T*/ SELECT postID, title, ptext, post_time, userID FROM posts"
        f" WHERE post_time > NOW() - INTERVAL {lookback} SECOND"
        " AND IFNULL(is_private,0)=0 ORDER BY post_time ASC LIMIT 30",
        (),
    )
    items = [
        {
            "kind": "post",
            "id": int(row[0]),
            "post_id": int(row[0]),
            "pcomment_id": 0,
            "title": str(row[1] or ""),
            "text": _plain(row[2]),
            "user_id": int(row[4] or 0),
            "position": f"帖子《{str(row[1] or '')[:60]}》正文",
        }
        for row in posts
    ]
    _, comments = await _query(
        "/*T*/ SELECT c.pcommentID, c.ptargetID, c.pctext, c.time, c.userID, p.title"
        " FROM pcomments c JOIN posts p ON p.postID=c.ptargetID"
        f" WHERE c.time > NOW() - INTERVAL {lookback} SECOND"
        " AND IFNULL(p.is_private,0)=0 ORDER BY c.time ASC LIMIT 30",
        (),
    )
    items += [
        {
            "kind": "comment",
            "id": int(row[0]),
            "post_id": int(row[1]),
            "pcomment_id": int(row[0]),
            "title": str(row[5] or ""),
            "text": _plain(row[2]),
            "user_id": int(row[4] or 0),
            "position": f"帖子《{str(row[5] or '')[:60]}》的评论",
        }
        for row in comments
    ]
    _, ccomments = await _query(
        "/*T*/ SELECT cc.ccommentID, cc.ctargetID, cc.cctext, cc.time, cc.userID,"
        " p.postID, p.title FROM ccomments cc"
        " JOIN pcomments pc ON pc.pcommentID = cc.ctargetID"
        " JOIN posts p ON p.postID = pc.ptargetID"
        f" WHERE cc.time > NOW() - INTERVAL {lookback} SECOND"
        " AND IFNULL(p.is_private,0)=0 ORDER BY cc.time ASC LIMIT 30",
        (),
    )
    items += [
        {
            "kind": "ccomment",
            "id": int(row[0]),
            "post_id": int(row[5]),
            "pcomment_id": int(row[1]),
            "title": str(row[6] or ""),
            "text": _plain(row[2]),
            "user_id": int(row[4] or 0),
            "position": f"帖子《{str(row[6] or '')[:60]}》的楼中楼回复",
        }
        for row in ccomments
    ]
    # 每轮取一次在途工单：同一帖的后续评论不再重复建单
    open_threads: dict[str, str] = {}
    if any(item["kind"] != "post" for item in items):
        open_threads = _open_threads(await _skein_tickets())
    for item in items:
        try:
            await handle(pool, item, open_threads)
        except Exception:
            log.exception("frontdesk item failed %s:%s", item["kind"], item["id"])


async def run_monitor() -> None:
    from app.config import get_config

    cfg = get_config()
    pool = await db.create_pool(cfg.pg_dsn, name="frontdesk", min_size=1, max_size=2)
    await pool.execute(DDL)
    interval = max(15, _env_int("FRONTDESK_INTERVAL", 60))
    log.info("frontdesk monitor on every %ss", interval)
    await asyncio.sleep(10)
    while True:
        try:
            await one_round(pool)
            await pool.execute(
                "DELETE FROM frontdesk_seen WHERE created_at < now() - interval '30 days'"
            )
        except Exception:
            log.exception("frontdesk round failed")
        await asyncio.sleep(interval)
