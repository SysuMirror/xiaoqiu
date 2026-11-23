"""论坛数据 → RAG 索引同步。

sync_full: 全量（帖子 + 公开帖的评论）→ 新集合首次灌库用
sync_recent: 增量（按 post_time）——仅帖子；评论随全量/定期重灌

用法:
  python -m app.rag.sync --full          # 全量 posts+comments
  python -m app.rag.sync --limit 300     # 增量帖子
"""
from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)

BATCH = 32


async def _fetch_rows(sql: str, params: tuple) -> list[tuple]:
    from app.tools.forum.sql_templates import _query

    _, rows = await _query(sql, params)
    return rows


def _doc(doc_id: str, source: str, title: str, content: str, post_time):
    return {
        "id": doc_id,
        "source": source,
        "title": title[:200],
        "content": content[:3000],
        "post_time": post_time,
    }


async def sync_full() -> int:
    """全量：帖子 + 隶属公开帖的评论。"""
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    from app.config import get_config
    from app.rag import store
    from app.tools.forum.sql_templates import init_forum_pool

    await init_forum_pool()
    await store.init_rag(get_config())

    posts = await _fetch_rows(
        "/*T*/ SELECT p.postID, p.title, p.ptext, p.post_time FROM posts p"
        " WHERE p.is_private = 0 ORDER BY p.postID",
        (),
    )
    docs = [_doc(f"post:{r[0]}", "forum_post", str(r[1] or ""), str(r[2] or ""), r[3]) for r in posts]

    comments = await _fetch_rows(
        "/*T*/ SELECT c.pcommentID, c.pctext, c.time, c.ptargetID FROM pcomments c"
        " JOIN posts p ON p.postID = c.ptargetID WHERE p.is_private = 0",
        (),
    )
    docs += [
        {**_doc(f"comment:{r[0]}", "forum_comment",
                f"评论于帖子{r[3]}", str(r[1] or ""), r[2]),
         "parent_post_id": f"post:{r[3]}"}
        for r in comments
    ]

    total = 0
    for i in range(0, len(docs), BATCH):
        total += await store.upsert_documents(docs[i : i + BATCH])
        if total % (BATCH * 20) < BATCH:
            log.info("synced %d/%d", total, len(docs))
    print(f"FULL sync done: {total} documents (posts={len(posts)}, comments={len(comments)})")
    await store.close_rag()
    return total


async def sync_recent(limit: int = 300) -> int:
    """增量：仅帖子（按 post_time）。"""
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    from app.config import get_config
    from app.rag import store
    from app.tools.forum.sql_templates import init_forum_pool

    await init_forum_pool()
    await store.init_rag(get_config())

    last = await store.last_post_time()
    if last:
        rows = await _fetch_rows(
            "/*T*/ SELECT p.postID, p.title, p.ptext, p.post_time FROM posts p"
            " WHERE p.post_time > %s AND p.is_private = 0"
            " ORDER BY p.post_time DESC LIMIT " + str(limit),
            (last,),
        )
    else:
        rows = await _fetch_rows(
            "/*T*/ SELECT p.postID, p.title, p.ptext, p.post_time FROM posts p"
            " WHERE p.is_private = 0 ORDER BY p.post_time DESC LIMIT " + str(limit),
            (),
        )

    docs = [_doc(f"post:{r[0]}", "forum_post", str(r[1] or ""), str(r[2] or ""), r[3]) for r in rows]
    total = 0
    for i in range(0, len(docs), BATCH):
        total += await store.upsert_documents(docs[i : i + BATCH])
    print(f"synced {total} documents (last_post_time={last})")
    await store.close_rag()
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from app.win_compat import setup_event_loop

    setup_event_loop()
    if "--full" in sys.argv:
        asyncio.run(sync_full())
    else:
        limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 300
        asyncio.run(sync_recent(limit))
