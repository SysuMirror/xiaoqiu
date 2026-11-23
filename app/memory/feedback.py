"""消息级反馈（赞/踩）：绑定 run_id，供效果评估与坏例回收。"""
from __future__ import annotations

from app import db

_pool: db.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS message_feedback (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    rating     SMALLINT NOT NULL,          -- 1 赞 / -1 踩
    comment    TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_time ON message_feedback (created_at DESC);
"""


async def init_feedback(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="feedback", min_size=1, max_size=2)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)


async def close_feedback() -> None:
    if _pool is not None:
        await _pool.close()


async def submit(run_id: str, thread_id: str, user_id: str, rating: int, comment: str) -> str:
    """提交反馈；重复 run 同用户为覆盖更新。返回 'created' | 'updated'。"""
    assert _pool is not None
    rating = 1 if rating >= 0 else -1
    comment = (comment or "").strip()[:500]
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM message_feedback WHERE run_id=$1 AND user_id=$2", run_id, user_id)
        if row:
            await conn.execute(
                "UPDATE message_feedback SET rating=$1, comment=$2, created_at=now()"
                " WHERE id=$3", rating, comment, row["id"])
            return "updated"
        await conn.execute(
            "INSERT INTO message_feedback (run_id, thread_id, user_id, rating, comment)"
            " VALUES ($1,$2,$3,$4,$5)", run_id, thread_id, user_id, rating, comment)
    return "created"


async def stats(days: int = 30) -> dict:
    assert _pool is not None
    row = await _pool.fetchrow(
        "SELECT count(*) FILTER (WHERE rating=1) AS up,"
        " count(*) FILTER (WHERE rating=-1) AS down"
        " FROM message_feedback WHERE created_at > now() - ($1 || ' days')::interval", str(days))
    return {"up": int(row["up"] or 0), "down": int(row["down"] or 0), "days": days}


async def list_recent(limit: int = 100) -> list[dict]:
    assert _pool is not None
    rows = await _pool.fetch(
        "SELECT run_id, thread_id, user_id, rating, comment, created_at"
        " FROM message_feedback ORDER BY created_at DESC LIMIT $1", max(1, min(limit, 500)))
    return [{"run_id": r["run_id"], "thread_id": r["thread_id"], "user_id": r["user_id"],
             "rating": int(r["rating"]), "comment": r["comment"],
             "created_at": r["created_at"].isoformat()} for r in rows]


async def list_for_user(user_id: str, limit: int = 100) -> list[dict]:
    assert _pool is not None
    rows = await _pool.fetch(
        "SELECT run_id, thread_id, rating, comment, created_at FROM message_feedback"
        " WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
        user_id, max(1, min(limit, 500)))
    return [{"run_id": r["run_id"], "thread_id": r["thread_id"], "rating": int(r["rating"]),
             "comment": r["comment"], "created_at": r["created_at"].isoformat()} for r in rows]


async def delete_user(user_id: str) -> int:
    n = await _pool.execute("DELETE FROM message_feedback WHERE user_id=$1", user_id)
    return int(n.split()[-1])
