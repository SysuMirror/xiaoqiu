"""统一 RAG 服务客户端（组内 group-rag，无鉴权）。

POST {service_url}/kb/search {"query","k","category"} → {results:[{source,category,content,rank,score}],total}
source 形如 market_<postID>.txt，可反查原帖。错误：category 非法 400 / 空 query 422 / 链路失败 502（可重试）。
"""
from __future__ import annotations

import logging
import re

import httpx

from app.config import get_config

log = logging.getLogger(__name__)

_POST_ID_RE = re.compile(r"^market_(\d+)\.txt$")


def source_post_id(source: str) -> str:
    """market_6336.txt → 6336；不匹配返回空串。"""
    m = _POST_ID_RE.match(source or "")
    return m.group(1) if m else ""


def post_url(post_id: str) -> str:
    return f"https://ssemarket.cn/new/postdetail/{post_id}"


async def search_chunks(query: str, k: int = 5, category: str | None = None) -> list[dict]:
    """调统一 RAG 服务检索切片。502 幂等可重试一次；服务不可用抛异常由调用方降级。

    返回 [{source, post_id, content, rank, score}]（按 rank 升序）。
    """
    cfg = get_config().rag
    base = (cfg.service_url or "").rstrip("/")
    payload = {"query": query, "k": max(1, min(int(k), 5)), "category": category or cfg.service_category}
    last_exc: Exception | None = None
    for attempt in (1, 2):  # 502 链路失败可重试
        try:
            async with httpx.AsyncClient(timeout=cfg.service_timeout_seconds, trust_env=False) as hc:
                resp = await hc.post(f"{base}/kb/search", json=payload)
            if resp.status_code == 502:
                raise httpx.HTTPStatusError("rag upstream 502", request=resp.request, response=resp)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in data.get("results", []):
                src = str(r.get("source", ""))
                results.append({
                    "source": src,
                    "post_id": source_post_id(src),
                    "content": str(r.get("content", "")),
                    "rank": int(r.get("rank", 0)),
                    "score": float(r.get("score", 0)),
                })
            results.sort(key=lambda x: x["rank"])
            return results
        except Exception as e:  # noqa: BLE001
            last_exc = e
            log.warning("rag service attempt %d failed: %s", attempt, e)
    raise last_exc  # type: ignore[misc]
