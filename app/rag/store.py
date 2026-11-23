"""RAG 存储：Qdrant（老师提供的 k3s 集群，经 frp 暴露）+ 远程 bge-m3 embedding（GPU）。

集合命名规范：{组前缀}_ssemarket_rag（组前缀 g3dfc5d61_，与既有 assist_v1 集合一致）。
embedding 1024 维（bge-m3），OpenAI 兼容 /v1/embeddings 接口。
dev 经 SSH 隧道（6333→25033, 13012→13011），生产在服务器上直连 127.0.0.1。
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import AppConfig

log = logging.getLogger(__name__)

EMBED_TIMEOUT = 20
_client: AsyncQdrantClient | None = None
_cfg: AppConfig | None = None


async def init_rag(cfg: AppConfig) -> None:
    global _client, _cfg
    _cfg = cfg
    import asyncio
    import os

    _client = AsyncQdrantClient(
        url=cfg.rag.qdrant_url,
        api_key=os.environ.get(cfg.rag.qdrant_api_key_env) or None,
        timeout=15,
    )
    last_err: Exception | None = None
    for attempt in range(1, 31):  # frp 链路约 2 分钟一切换的间歇抖动，覆盖至少一个完整窗口
        try:
            await _client.get_collections()
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("qdrant connect attempt %d failed: %s", attempt, e)
            await asyncio.sleep(min(2 * attempt, 5))
    if last_err:
        raise last_err
    if not await _client.collection_exists(cfg.rag.collection):
        await _client.create_collection(
            collection_name=cfg.rag.collection,
            vectors_config=VectorParams(size=cfg.rag.embed_dim, distance=Distance.COSINE),
        )
        log.info("created qdrant collection %s (dim=%d)", cfg.rag.collection, cfg.rag.embed_dim)
    from qdrant_client.models import IntegerIndexParams, IntegerIndexType

    await _client.create_payload_index(
        collection_name=cfg.rag.collection,
        field_name="post_time",
        field_schema=IntegerIndexParams(type=IntegerIndexType.INTEGER, range=True),
    )


async def close_rag() -> None:
    global _client
    if _client is not None:
        await _client.close()
    _client = None


def client() -> AsyncQdrantClient | None:
    """共享 qdrant 客户端（记忆向量库等复用同一连接；未初始化返回 None）。"""
    return _client


async def embed(texts: list[str]) -> list[list[float]]:
    assert _cfg is not None
    async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as hc:
        resp = await hc.post(
            f"{_cfg.rag.embed_base_url}/v1/embeddings",
            json={"input": texts[:32]},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [item["embedding"] for item in data]


def _point_id(doc_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sse-rag:{doc_id}"))


async def upsert_documents(docs: list[dict]) -> int:
    """docs: [{id, source, title, content, post_time(datetime)}]，批量 embedding 后 upsert。"""
    assert _client is not None and _cfg is not None
    vecs = await embed([f"{d['title']}\n{d['content'][:500]}" for d in docs])
    points = [
        PointStruct(
            id=_point_id(d["id"]),
            vector=v,
            payload={
                "doc_id": d["id"],
                "source": d["source"],
                "title": d["title"],
                "content": d["content"][:2000],
                "post_time": int(d["post_time"].timestamp())
                if isinstance(d.get("post_time"), datetime) else 0,
                "parent_post_id": d.get("parent_post_id", ""),
            },
        )
        for d, v in zip(docs, vecs)
    ]
    await _client.upsert(collection_name=_cfg.rag.collection, points=points, wait=True)
    return len(points)


async def last_post_time() -> datetime | None:
    assert _client is not None and _cfg is not None
    points, _ = await _client.scroll(
        collection_name=_cfg.rag.collection,
        limit=1,
        with_payload=["doc_id", "post_time"],
        order_by={"key": "post_time", "direction": "desc"},
    )
    if not points:
        return None
    ts = (points[0].payload or {}).get("post_time", 0)
    return datetime.fromtimestamp(ts, tz=UTC) if ts else None


async def search(query: str, top_k: int = 5) -> list[dict]:
    assert _client is not None and _cfg is not None
    [qv] = await embed([query])
    res = await _client.query_points(
        collection_name=_cfg.rag.collection,
        query=qv,
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "id": (h.payload or {}).get("doc_id", ""),
            "source": (h.payload or {}).get("source", ""),
            "title": (h.payload or {}).get("title", ""),
            "content": (h.payload or {}).get("content", "")[:300],
            "score": round(h.score, 3),
        }
        for h in res.points
    ]


async def delete_points(doc_ids: list[str]) -> int:
    """按 doc_id 删除点（webhook：帖子删除/转私密、评论删除）。"""
    assert _client is not None and _cfg is not None
    if not doc_ids:
        return 0
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    await _client.delete(
        collection_name=_cfg.rag.collection,
        points_selector=Filter(must=[
            FieldCondition(key="doc_id", match=MatchAny(any=doc_ids)),
        ]),
        wait=True,
    )
    return len(doc_ids)


async def delete_by_parent(parent_post_id: str) -> int:
    """删除某帖子的全部评论点（parent_post_id = "post:<postID>"）。"""
    assert _client is not None and _cfg is not None
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    res = await _client.delete(
        collection_name=_cfg.rag.collection,
        points_selector=Filter(must=[
            FieldCondition(key="parent_post_id", match=MatchValue(value=parent_post_id)),
        ]),
        wait=True,
    )
    return getattr(res, "operation_id", 0) is not None and 1 or 1


async def iter_doc_ids(page_size: int = 256):
    """滚动全部 (doc_id, source, parent_post_id)，对账用。"""
    assert _client is not None and _cfg is not None
    offset = None
    while True:
        points, offset = await _client.scroll(
            collection_name=_cfg.rag.collection,
            limit=page_size,
            with_payload=["doc_id", "source", "parent_post_id"],
            offset=offset,
        )
        for pt in points:
            pl = pt.payload or {}
            yield pl.get("doc_id", ""), pl.get("source", ""), pl.get("parent_post_id", "")
        if offset is None:
            break
