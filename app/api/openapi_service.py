"""开放 API（v3 能力输出）：X-API-Key 鉴权，把小秋的检索能力开放给其他项目调用。

- 密钥：PG openapi_keys 表（sha256 哈希存储）；签发脚本 scripts/openkey_mint.py
- 限频：进程内滑动窗口 per_minute + PG 按日计数（daily_limit，默认 openapi.daily_default）
- 能力面 v1：POST /api/v1/agent/open/kb_search（统一 RAG 检索，服务端强制引用格式）
经 nginx 既有 /api/v1/agent/ 路由暴露；鉴权失败/超限一律 4xx，密钥可随时 revoke。
"""
from __future__ import annotations

import hashlib
import logging

from app import db
from fastapi import Request
from fastapi.responses import JSONResponse

from app.common.limiter import SlidingWindowLimiter
from app.config import get_config

log = logging.getLogger(__name__)

_pool: db.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS openapi_keys (
    key_hash    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    owner       TEXT NOT NULL DEFAULT '',
    daily_limit INT NOT NULL DEFAULT 1000,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS openapi_usage (
    key_hash TEXT NOT NULL,
    day      DATE NOT NULL DEFAULT current_date,
    count    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (key_hash, day)
);
"""

_LIMITER = SlidingWindowLimiter()


async def init_openapi(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="openapi", min_size=0, max_size=1)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)


async def close_openapi() -> None:
    if _pool is not None:
        await _pool.close()


def key_hash(raw_key: str) -> str:
    return hashlib.sha256((raw_key or "").strip().encode()).hexdigest()


async def issue_key(name: str, owner: str = "", daily_limit: int | None = None) -> str:
    """签发新 key（明文只返回一次；库里只存哈希）。池未初始化时惰性建（CLI 裸跑用）。"""
    global _pool
    if _pool is None:
        await init_openapi(get_config().pg_dsn)
    import uuid as _uuid

    raw = f"xqk_{_uuid.uuid4().hex}"
    limit = daily_limit or get_config().openapi.daily_default
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO openapi_keys (key_hash, name, owner, daily_limit) VALUES ($1,$2,$3,$4)",
            key_hash(raw), name[:60], owner[:60], max(1, int(limit)))
    return raw


async def revoke_key(name: str) -> bool:
    assert _pool is not None
    n = await _pool.execute("UPDATE openapi_keys SET revoked=TRUE WHERE name=$1", name)
    return n.endswith("1")


async def _authenticate(request: Request) -> tuple[str | None, JSONResponse | None]:
    cfg = get_config().openapi
    raw = request.headers.get("X-API-Key", "")
    if not raw:
        return None, JSONResponse(status_code=401, content={"error": "missing X-API-Key"})
    h = key_hash(raw)
    assert _pool is not None
    row = await _pool.fetchrow(
        "SELECT key_hash, daily_limit FROM openapi_keys WHERE key_hash=$1 AND revoked=FALSE", h)
    if row is None:
        return None, JSONResponse(status_code=401, content={"error": "invalid api key"})

    ok, _ = _LIMITER.allow(f"open:{h}", max(1, cfg.per_minute))
    if not ok:
        return None, JSONResponse(status_code=429, content={"error": "rate limit (per minute)"})

    used = await _pool.fetchval(
        "INSERT INTO openapi_usage (key_hash, day, count) VALUES ($1, current_date, 1)"
        " ON CONFLICT (key_hash, day) DO UPDATE SET count = openapi_usage.count + 1"
        " RETURNING count", h)
    if int(used or 0) > int(row["daily_limit"]):
        return None, JSONResponse(status_code=429, content={"error": "daily quota exceeded"})
    await _pool.execute("UPDATE openapi_keys SET last_used_at=now() WHERE key_hash=$1", h)
    return h, None


async def open_kb_search(request: Request):
    """开放检索：与内部 kb_search 同源，强制附引用格式，便于调用方溯源。"""
    _key_id, err = await _authenticate(request)
    if err is not None:
        return err
    try:
        body = await request.json()
        query = str(body.get("query", "")).strip()[:300]
        k = max(1, min(int(body.get("k", 5)), 5))
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=422, content={"error": "query 必填"})
    if not query:
        return JSONResponse(status_code=422, content={"error": "query 必填"})
    from app.rag import store as rag_store
    from app.rag import unified
    from app.security.injection import scan_and_report

    results = []
    try:
        hits = await unified.search_chunks(query, k)
    except Exception:  # noqa: BLE001 —— 统一服务故障降级本机 qdrant（与 kb_search 工具同链）
        try:
            local = await rag_store.search(query, k)
        except Exception:  # noqa: BLE001 —— 双链全断如实 502（可重试）
            return JSONResponse(status_code=502, content={"error": "retrieval upstream unavailable"})
        if not local:
            return {"query": query, "total": 0, "results": []}
        for hres in local:
            scan_and_report(str(hres.get("content", "")), "open_api_local")
            results.append({
                "post_id": hres["id"].split(":")[1],
                "url": unified.post_url(hres["id"].split(":")[1]),
                "content": str(hres.get("content", ""))[:400],
                "rank": len(results),
            })
        return {"query": query, "total": len(results), "results": results}
    for hres in hits:
        if not hres.get("post_id"):
            continue
        scan_and_report(str(hres.get("content", "")), "open_api")
        results.append({
            "post_id": hres["post_id"],
            "url": unified.post_url(hres["post_id"]),
            "content": str(hres.get("content", ""))[:400],
            "rank": hres.get("rank", 0),
        })
    return {"query": query, "total": len(results), "results": results}
