"""Run 生命周期：持久化 run 实体（SSE 只是观察通道）。"""
from __future__ import annotations

import logging
import uuid

import asyncpg

from app import db

log = logging.getLogger(__name__)

_pool: db.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    status      TEXT NOT NULL,   -- running/succeeded/failed/cancelled
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_user_active ON runs (user_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);
"""

# v3 成本归因：逐 run token 总量 / 首 token 时延 / 模型（用量报表与 SLO 数据源）
MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS tokens_total INT NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS first_token_ms INT",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs (user_id, created_at DESC)",
)


async def init_runs(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="runs", min_size=1, max_size=2)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)
        for stmt in MIGRATIONS:
            await conn.execute(stmt)


async def close_runs() -> None:
    if _pool is not None:
        await _pool.close()


async def create(thread_id: str, user_id: str) -> str:
    run_id = uuid.uuid4().hex[:16]
    await _pool.execute(
        "INSERT INTO runs (run_id, thread_id, user_id, status) VALUES ($1,$2,$3,'running')",
        run_id, thread_id, user_id,
    )
    return run_id


async def finish(run_id: str, status: str, error: str | None = None) -> None:
    """仅当仍为 running 时落终态（防终态被覆盖）。interrupt 保持 running 语义不受影响。"""
    await _pool.execute(
        "UPDATE runs SET status=$2, error=$3, finished_at=now() WHERE run_id=$1 AND status='running'",
        run_id, status, error,
    )


async def finish_metrics(run_id: str, tokens: int, first_token_ms: int | None, model: str) -> None:
    """成功结束时补记成本/时延数据（只写本次观测值，幂等无害）。"""
    await _pool.execute(
        "UPDATE runs SET tokens_total=$2, first_token_ms=$3, model=$4 WHERE run_id=$1",
        run_id, max(0, int(tokens or 0)),
        int(first_token_ms) if first_token_ms is not None else None, (model or "")[:64],
    )


async def usage_by_day(user_id: str, days: int = 30) -> list[dict]:
    """按日聚合用户 token 用量（成本归因报表）。"""
    rows = await _pool.fetch(
        "SELECT created_at::date AS day, count(*) AS runs,"
        " COALESCE(sum(tokens_total),0) AS tokens"
        " FROM runs WHERE user_id=$1 AND created_at > now() - ($2 || ' days')::interval"
        " GROUP BY 1 ORDER BY 1 DESC",
        user_id, str(max(1, min(int(days), 365))),
    )
    return [{"day": str(r["day"]), "runs": int(r["runs"]), "tokens": int(r["tokens"])}
            for r in rows]


async def active_count_all() -> int:
    return await _pool.fetchval("SELECT COUNT(*) FROM runs WHERE status='running'")


async def active_count(user_id: str, exclude_thread: str | None = None) -> int:
    """用户在跑的 run 数。exclude_thread：同 thread 重发场景下，旧 run 刚被顶替、
    cancelled 终态是异步落库的——计数先排除本 thread，防并发检查误判 429。"""
    if exclude_thread:
        return await _pool.fetchval(
            "SELECT COUNT(*) FROM runs WHERE user_id=$1 AND status='running' AND thread_id<>$2",
            user_id, exclude_thread)
    return await _pool.fetchval(
        "SELECT COUNT(*) FROM runs WHERE user_id=$1 AND status='running'", user_id
    )


async def quarantine_orphans() -> int:
    """启动对账（XR-1）：上次进程遗留的 running → resumable（不弃置——checkpoint
    在 PG，可经 /task/recover 从最后 checkpoint 续跑；扫描幂等）。
    历史：此前一律标 failed(server_restart)，发版即杀全部活跃 run——XR-1 起改为
    可恢复态；恢复不了的场景由 /task/recover 的 try/except 兜底（宁可弃恢复
    不可错恢复）。"""
    n = await _pool.execute(
        "UPDATE runs SET status='resumable', error='server_restart'"
        " WHERE status='running'"
    )
    count = int(n.split()[-1])
    if count:
        log.warning("quarantined %d orphan runs as resumable", count)
    return count


async def retire(run_id: str, error: str) -> None:
    """恢复接替时退役旧 run（仅 resumable 可退役；审计上不算失败，
    error 记录接替事实）。"""
    await _pool.execute(
        "UPDATE runs SET status='failed', error=$2, finished_at=now()"
        " WHERE run_id=$1 AND status='resumable'",
        run_id, error,
    )


async def get(thread_id: str) -> asyncpg.Record | None:
    return await _pool.fetchrow(
        "SELECT run_id, thread_id, user_id, status, error, created_at, finished_at"
        " FROM runs WHERE thread_id=$1 ORDER BY created_at DESC LIMIT 1",
        thread_id,
    )


async def user_totals(user_id: str) -> dict:
    row = await _pool.fetchrow(
        "SELECT count(*) AS runs, COALESCE(sum(tokens_total),0) AS tokens FROM runs WHERE user_id=$1",
        user_id)
    return {"runs": int(row["runs"]), "tokens": int(row["tokens"])}


async def delete_user(user_id: str) -> int:
    n = await _pool.execute("DELETE FROM runs WHERE user_id=$1", user_id)
    return int(n.split()[-1])
