"""行动台账：append-only，所有 L2/L3 工具与主动 job 的行为记录（兼审计）。"""
from __future__ import annotations

import json
import logging

from app import db

log = logging.getLogger(__name__)

_pool: db.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS actions (
    id           BIGSERIAL PRIMARY KEY,
    actor        TEXT NOT NULL,          -- 'system' 或 user 标识
    action_type  TEXT NOT NULL,          -- kill_process / daily_digest / ...
    target       TEXT,
    args_summary JSONB,
    result       TEXT NOT NULL,          -- success / rejected / failed
    detail       TEXT,
    thread_id    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_actions_created ON actions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_thread_type ON actions (thread_id, action_type);
"""


async def init_ledger(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="ledger", min_size=1, max_size=3)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)


async def close_ledger() -> None:
    if _pool is not None:
        await _pool.close()


async def record(
    actor: str,
    action_type: str,
    result: str,
    target: str | None = None,
    args_summary: dict | None = None,
    detail: str | None = None,
    thread_id: str | None = None,
) -> None:
    """best-effort：台账失败只记日志+告警语义，不阻断业务（ADR-7）。"""
    try:
        assert _pool is not None, "ledger not initialized"
        await _pool.execute(
            "INSERT INTO actions (actor, action_type, target, args_summary, result, detail, thread_id)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7)",
            actor, action_type, target,
            json.dumps(args_summary or {}, ensure_ascii=False),
            result, detail, thread_id,
        )
    except Exception:
        log.exception("ledger write failed (action=%s target=%s)", action_type, target)


async def count_today(action_type: str, actor: str) -> int:
    """当日成功执行计数（写侧工具限频依据，UTC 日界）。"""
    assert _pool is not None, "ledger not initialized"
    return int(await _pool.fetchval(
        "SELECT count(*) FROM actions WHERE action_type=$1 AND actor=$2 AND result='success'"
        " AND created_at >= date_trunc('day', now())",
        action_type, actor,
    ))


async def record_tool_exec(thread_id: str, tool_call_id: str, tool: str,
                           content: str) -> None:
    """XR-2 工具幂等台账：非只读工具成功执行按 (thread_id, tool_call_id) 落账。
    tool_call_id 是模型生成的调用标识，checkpoint 重放时同一 id 重发——重放
    对账用；best-effort 与 record 同语义（失败不阻断）。"""
    await record(
        "agent", "tool_exec", "success", target=tool,
        args_summary={"call_id": tool_call_id},
        detail=(content or "")[:800], thread_id=thread_id,
    )


async def find_tool_exec(thread_id: str, tool_call_id: str) -> str | None:
    """幂等对账：该 tool_call 已成功执行过则返回缓存结果原文，否则 None。"""
    assert _pool is not None, "ledger not initialized"
    return await _pool.fetchval(
        "SELECT detail FROM actions"
        " WHERE thread_id=$1 AND action_type='tool_exec' AND result='success'"
        " AND args_summary->>'call_id'=$2 ORDER BY id DESC LIMIT 1",
        thread_id, tool_call_id,
    )


async def anonymize_actor(actor: str) -> int:
    """用户删除权：台账保留（审计要求）但剥离个人内容与身份。"""
    assert _pool is not None, "ledger not initialized"
    n = await _pool.execute(
        "UPDATE actions SET actor='deleted_user', detail=NULL, args_summary='{}'::jsonb"
        " WHERE actor=$1", actor)
    return int(n.split()[-1])


async def recent(limit: int = 10) -> list[dict]:
    assert _pool is not None, "ledger not initialized"
    rows = await _pool.fetch(
        "SELECT actor, action_type, target, result, detail, created_at"
        " FROM actions ORDER BY created_at DESC LIMIT $1",
        limit,
    )
    return [
        {
            "actor": r["actor"],
            "action_type": r["action_type"],
            "target": r["target"],
            "result": r["result"],
            "detail": r["detail"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
