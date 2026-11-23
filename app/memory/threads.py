"""thread 属主绑定 + 会话中心数据层：列表/改名/软删/触活/最近会话。"""
from __future__ import annotations

from app import db

_pool: db.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    closed     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 会话中心扩展列（title 手动/自动改名；updated_at 列表排序；deleted 软删；
# source 来源面——ops 会话与 chat 会话共表分列，列表按来源过滤）
MIGRATIONS = (
    "ALTER TABLE threads ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE threads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE threads ADD COLUMN IF NOT EXISTS deleted BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE threads ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'chat'",
    (
        "CREATE INDEX IF NOT EXISTS idx_threads_user_live ON threads (user_id, updated_at DESC)"
        " WHERE deleted = FALSE"
    ),
)


async def init_threads(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="threads", min_size=1, max_size=3)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)
        await conn.execute(
            "ALTER TABLE threads ADD COLUMN IF NOT EXISTS closed BOOLEAN NOT NULL DEFAULT FALSE"
        )
        for stmt in MIGRATIONS:
            await conn.execute(stmt)


async def close_threads() -> None:
    if _pool is not None:
        await _pool.close()


async def bind_or_verify(thread_id: str, user_id: str, role: str,
                         source: str = "chat") -> None:
    """首次使用则绑定属主；已存在则校验属主一致，不一致抛 403。软删会话拒绝复用。"""
    assert _pool is not None, "threads store not initialized"
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, closed, deleted FROM threads WHERE thread_id=$1", thread_id)
        if row is None:
            await conn.execute(
                "INSERT INTO threads (thread_id, user_id, role, source) VALUES ($1,$2,$3,$4)"
                " ON CONFLICT (thread_id) DO NOTHING",
                thread_id, user_id, role, source,
            )
        elif row["deleted"]:
            from fastapi import HTTPException

            raise HTTPException(status_code=410, detail="会话已删除")
        elif row["user_id"] != user_id:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="thread 属主校验失败")
        elif row["closed"]:
            from fastapi import HTTPException

            raise HTTPException(status_code=410, detail="会话已关闭，请重新 bootstrap 开启新会话")


async def owner_of(thread_id: str) -> str:
    """返回属主 user_id；不存在返回空串（读路径用，不产生副作用）。"""
    if _pool is None:
        return ""
    row = await _pool.fetchrow("SELECT user_id FROM threads WHERE thread_id=$1", thread_id)
    return (row["user_id"] if row else "") or ""


async def touch(thread_id: str) -> None:
    """有新消息时刷新 updated_at（会话列表排序依据）。"""
    if _pool is None:
        return
    await _pool.execute("UPDATE threads SET updated_at=now() WHERE thread_id=$1", thread_id)


async def get_title(thread_id: str) -> str:
    if _pool is None:
        return ""
    row = await _pool.fetchrow("SELECT title FROM threads WHERE thread_id=$1", thread_id)
    return (row["title"] if row else "") or ""


async def set_title(thread_id: str, user_id: str, title: str) -> bool:
    """手动改名（校验属主）。返回是否生效。"""
    if _pool is None:
        return False
    title = title.strip()[:60]
    if not title:
        return False
    tag = await _pool.execute(
        "UPDATE threads SET title=$1 WHERE thread_id=$2 AND user_id=$3 AND deleted=FALSE",
        title, thread_id, user_id)
    return tag.endswith("1")


async def soft_delete(thread_id: str, user_id: str) -> bool:
    if _pool is None:
        return False
    tag = await _pool.execute(
        "UPDATE threads SET deleted=TRUE WHERE thread_id=$1 AND user_id=$2", thread_id, user_id)
    return tag.endswith("1")


async def latest_thread(user_id: str) -> str:
    """用户最近活跃的未删未关会话；无则空串（前端开新会话）。"""
    if _pool is None:
        return ""
    row = await _pool.fetchrow(
        "SELECT thread_id FROM threads WHERE user_id=$1 AND deleted=FALSE AND closed=FALSE"
        " ORDER BY updated_at DESC LIMIT 1", user_id)
    return row["thread_id"] if row else ""


async def list_threads(user_id: str, limit: int = 50, offset: int = 0,
                       source: str | None = None) -> list[dict]:
    assert _pool is not None
    q = ("SELECT thread_id, title, created_at, updated_at FROM threads"
         " WHERE user_id=$1 AND deleted=FALSE")
    args: list = [user_id]
    if source:
        q += f" AND source=${len(args) + 1}"
        args.append(source)
    q += f" ORDER BY updated_at DESC LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}"
    args += [max(1, min(limit, 100)), max(0, offset)]
    rows = await _pool.fetch(q, *args)
    return [{
        "session_id": r["thread_id"],
        "title": r["title"] or "",
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    } for r in rows]


async def close_thread(thread_id: str) -> None:
    assert _pool is not None
    await _pool.execute("UPDATE threads SET closed=TRUE WHERE thread_id=$1", thread_id)


# langgraph AsyncPostgresSaver 物理表（用户删除权 / 保留期清理共用）
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def all_thread_ids(user_id: str) -> list[str]:
    assert _pool is not None
    rows = await _pool.fetch("SELECT thread_id FROM threads WHERE user_id=$1", user_id)
    return [r["thread_id"] for r in rows]


async def purge_threads(thread_ids: list[str]) -> None:
    """物理清除会话（含 checkpoint 三表）。调用方保证属主校验。"""
    assert _pool is not None
    if not thread_ids:
        return
    async with _pool.acquire() as conn, conn.transaction():
        for table in _CHECKPOINT_TABLES:
            await conn.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY($1)", thread_ids)
        await conn.execute("DELETE FROM threads WHERE thread_id = ANY($1)", thread_ids)
