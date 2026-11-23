"""数据生命周期清理（v3）：软删会话物理清除（含 checkpoints）/ run 与台账保留期清理。

每日一次，幂等；只清小秋自己的数据（PG agent 库），不碰论坛 MySQL。
删除顺序：先 checkpoints（langgraph 三表）后 threads 行，失败即停（下次重跑补齐）。
"""
from __future__ import annotations

import logging

from app import db

from app.config import get_config
from app.memory import ledger

log = logging.getLogger(__name__)

_pool: db.Pool | None = None

# langgraph AsyncPostgresSaver 的物理表（thread_id 关联删除）
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def init_retention(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="retention", min_size=0, max_size=2, command_timeout=None)


async def close_retention() -> None:
    if _pool is not None:
        await _pool.close()


async def purge_deleted_threads(days: int) -> int:
    """软删超过 N 天的会话：清 checkpoints 三表 + threads 行。返回清除会话数。"""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT thread_id FROM threads WHERE deleted = TRUE"
            " AND updated_at < now() - ($1 || ' days')::interval LIMIT 500",
            str(max(1, int(days))))
        ids = [r["thread_id"] for r in rows]
        if not ids:
            return 0
        async with conn.transaction():
            for table in _CHECKPOINT_TABLES:
                await conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ANY($1)", ids)
            await conn.execute("DELETE FROM threads WHERE thread_id = ANY($1)", ids)
    return len(ids)


async def prune_table(table: str, days: int, ts_col: str = "created_at") -> int:
    assert _pool is not None
    n = await _pool.execute(
        f"DELETE FROM {table} WHERE {ts_col} < now() - ($1 || ' days')::interval",
        str(max(1, int(days))))
    return int(n.split()[-1])


async def run_retention(force: bool = False) -> dict:
    """保留期清理入口（调度每日调用；幂等：无到期数据即 no-op）。"""
    cfg = get_config().retention
    if not cfg.enabled and not force:
        return {"result": "disabled"}
    result: dict = {}
    try:
        result["threads_purged"] = await purge_deleted_threads(cfg.deleted_threads_days)
        result["runs_pruned"] = await prune_table("runs", cfg.runs_days)
        result["actions_pruned"] = await prune_table("actions", cfg.actions_days)
        await ledger.record("system", "retention", "success", detail=str(result))
        log.info("retention done: %s", result)
        return {"result": "success", **result}
    except Exception as e:
        log.exception("retention failed")
        await ledger.record("system", "retention", "failed", detail=str(e)[:200])
        return {"result": "failed", "error": str(e)[:200]}
