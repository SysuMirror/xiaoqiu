"""用户语义记忆管理：写时合并（LLM 判定）+ hash 去重 + 容量 50 LRU + TTL + 向量语义召回。

存储：LangGraph PostgresStore（(user_id,"memories") 命名空间）为事实源；
qdrant 集合 g3dfc5d61_xiaoqiu_memory 为向量镜像（payload: user_id/key/text/updated_at），
recall 时按当前问题语义检索 top-k，与最近更新合并去重——替代旧的"取最近 5 条"。
向量镜像任何故障都降级为按更新时间取最近（宁近勿错，不阻断对话）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from langchain_core.messages import HumanMessage

from app.model.pool import ModelPool

log = logging.getLogger(__name__)

CAPACITY = 50
TTL_DAYS = 90

JUDGE_PROMPT = """你是记忆库管理员。已有记忆条目：
{items}

新记忆：「{text}」

判断新记忆应如何处理，只输出 JSON：
- {{"action":"duplicate","key":"..."}} 与某条完全重复
- {{"action":"update","key":"...","text":"合并后的完整文本"}} 是某条的更新/补充
- {{"action":"new"}} 全新信息
"""


class UserMemoryManager:
    def __init__(self, store, pool: ModelPool):
        self._store = store
        self._pool = pool
        # 向量后端由 runtime 注入（rag.store.client + embed），缺失时自动降级
        self._embed = None
        self._qdrant = None
        self._collection = ""
        self._recall_k = 5
        self._min_score = 0.35

    def setup_vectors(self, embed_fn, qdrant_client, collection: str,
                      dim: int = 1024, recall_k: int = 5, min_score: float = 0.35) -> None:
        self._embed = embed_fn
        self._qdrant = qdrant_client
        self._collection = collection
        self._recall_k = recall_k
        self._min_score = min_score

    @staticmethod
    def namespace(user_id: str) -> tuple[str, str]:
        return (user_id, "memories")

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.strip().encode()).hexdigest()[:16]

    @staticmethod
    def _point_id(user_id: str, key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"xiaoqiu-mem:{user_id}:{key}"))

    # ---------- 向量镜像 ----------

    async def _vec_upsert(self, user_id: str, key: str, text: str, updated_at: str) -> None:
        if not (self._embed and self._qdrant):
            return
        try:
            [vec] = await self._embed([text])
            from qdrant_client.models import PointStruct

            await self._qdrant.upsert(
                collection_name=self._collection,
                points=[PointStruct(
                    id=self._point_id(user_id, key),
                    vector=vec,
                    payload={"user_id": user_id, "key": key, "text": text, "updated_at": updated_at},
                )],
                wait=False,
            )
        except Exception as e:  # noqa: BLE001 —— 镜像失败不影响事实源
            log.warning("memory vector upsert failed: %s", e)

    async def _vec_delete(self, user_id: str, key: str) -> None:
        if not (self._embed and self._qdrant):
            return
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            await self._qdrant.delete(
                collection_name=self._collection,
                points_selector=Filter(must=[FieldCondition(key="key", match=MatchValue(value=key)),
                                             FieldCondition(key="user_id", match=MatchValue(value=user_id))]),
                wait=False,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("memory vector delete failed: %s", e)

    async def ensure_collection(self) -> None:
        """启动时建集合 + user_id 过滤索引（幂等）。"""
        if not (self._embed and self._qdrant):
            return
        try:
            from qdrant_client.models import Distance, VectorParams

            if not await self._qdrant.collection_exists(self._collection):
                await self._qdrant.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
                )
            from qdrant_client.models import KeywordIndexParams, KeywordIndexType

            await self._qdrant.create_payload_index(
                collection_name=self._collection,
                field_name="user_id",
                field_schema=KeywordIndexParams(type=KeywordIndexType.KEYWORD),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("memory collection init failed (recall 将降级为按时间): %s", e)

    async def recall(self, user_id: str, query: str, n: int = 5) -> list[str]:
        """语义召回：向量 top-k ∪ 最近更新，去重后截 n 条。任何故障降级 recent_for_prompt。"""
        recent = await self.recent_for_prompt(user_id, n)
        if not (self._embed and self._qdrant) or not query.strip():
            return recent
        try:
            [qv] = await self._embed([query[:300]])
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            res = await self._qdrant.query_points(
                collection_name=self._collection,
                query=qv,
                limit=n,
                query_filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]),
                with_payload=True,
            )
            hits = [(p.payload or {}).get("text", "") for p in res.points
                    if p.score >= self._min_score and (p.payload or {}).get("text")]
            merged = list(dict.fromkeys(hits + recent))
            return merged[:n]
        except Exception as e:  # noqa: BLE001
            log.warning("memory recall degraded to recent: %s", e)
            return recent

    # ---------- 事实源（PostgresStore） ----------

    async def list_all(self, user_id: str) -> list[dict]:
        """返回 [{key, text, updated_at}]，按 updated_at 升序（最老在前）。"""
        items = await self._store.asearch(self.namespace(user_id), limit=200)
        now = datetime.now(UTC)
        result = []
        for it in items:
            val = it.value or {}
            try:
                updated = datetime.fromisoformat(val.get("updated_at", "1970-01-01T00:00:00+00:00"))
            except ValueError:
                updated = now.replace(tzinfo=None)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if now - updated > timedelta(days=TTL_DAYS):
                await self._store.adelete(self.namespace(user_id), it.key)  # TTL 清理
                await self._vec_delete(user_id, it.key)
                continue
            result.append({
                "key": it.key,
                "text": val.get("text", ""),
                "hash": val.get("hash", ""),
                "updated_at": updated,
            })
        result.sort(key=lambda x: x["updated_at"])
        return result

    async def save(self, user_id: str, text: str, thread_id: str | None = None) -> str:
        """保存一条记忆，返回动作：new / merged / updated / duplicate / rejected。"""
        text = text.strip()[:500]
        if not text:
            return "rejected"
        existing = await self.list_all(user_id)

        # hash 去重
        h = self._hash(text)
        for it in existing:
            if it["hash"] == h:
                return "duplicate"

        # LLM 判定 merge/update/new，失败降级 new
        action, key, merged = await self._judge(existing, text)

        now = datetime.now(UTC).isoformat()
        if action in ("update", "merge") and key:
            await self._store.aput(self.namespace(user_id), key, {
                "text": merged or text, "hash": self._hash(merged or text), "updated_at": now,
            })
            await self._vec_upsert(user_id, key, merged or text, now)
            await self._enforce_capacity(user_id)
            return "merged" if action == "merge" else "updated"

        key = f"m_{h[:12]}_{len(existing)}"
        await self._store.aput(self.namespace(user_id), key, {
            "text": text, "hash": h, "updated_at": now,
        })
        await self._vec_upsert(user_id, key, text, now)
        await self._enforce_capacity(user_id)
        return "new"

    async def delete(self, user_id: str, key: str) -> bool:
        """用户主动删除一条记忆。返回是否存在。"""
        items = await self.list_all(user_id)
        if not any(it["key"] == key for it in items):
            return False
        await self._store.adelete(self.namespace(user_id), key)
        await self._vec_delete(user_id, key)
        return True

    async def delete_all(self, user_id: str) -> int:
        """用户删除权：清空该用户全部记忆（事实源 + 向量镜像）。返回清除条数。"""
        items = await self.list_all(user_id)
        for it in items:
            await self._store.adelete(self.namespace(user_id), it["key"])
            await self._vec_delete(user_id, it["key"])
        return len(items)

    async def update(self, user_id: str, key: str, text: str) -> bool:
        """用户主动改写一条记忆文本（重算 hash 与向量）。"""
        text = text.strip()[:500]
        if not text:
            return False
        items = await self.list_all(user_id)
        if not any(it["key"] == key for it in items):
            return False
        now = datetime.now(UTC).isoformat()
        await self._store.aput(self.namespace(user_id), key, {
            "text": text, "hash": self._hash(text), "updated_at": now,
        })
        await self._vec_upsert(user_id, key, text, now)
        return True

    async def _judge(self, existing: list[dict], text: str) -> tuple[str, str | None, str | None]:
        if not existing:
            return "new", None, None
        items = "\n".join(f'- key={it["key"]}: {it["text"]}' for it in existing[-20:])
        try:
            resp = await self._pool.ainvoke([
                HumanMessage(content=JUDGE_PROMPT.format(items=items, text=text))
            ])
            data = json.loads(resp.content.strip().strip("`").removeprefix("json").strip())
            action = data.get("action")
            if action == "duplicate":
                return "duplicate", None, None
            if action in ("update", "merge") and data.get("key"):
                return action, data.get("key"), (data.get("text") or text)
        except Exception as e:  # noqa: BLE001 —— LLM/解析失败降级为新增
            log.warning("memory judge failed, fallback to new: %s", e)
        return "new", None, None

    async def _enforce_capacity(self, user_id: str) -> None:
        items = await self.list_all(user_id)
        for it in items[: max(0, len(items) - CAPACITY)]:  # 最老的先淘汰（LRU 简化为写时排序）
            await self._store.adelete(self.namespace(user_id), it["key"])
            await self._vec_delete(user_id, it["key"])

    async def recent_for_prompt(self, user_id: str, n: int = 5) -> list[str]:
        items = await self.list_all(user_id)
        return [it["text"] for it in items[-n:]]
