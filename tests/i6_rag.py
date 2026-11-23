"""I6 验收：RAG 检索 + 引用溯源。

G1 增量同步：sync_recent(50) → documents 表有数据
G2 语义检索：中文 query 召回语义相关帖子（分数排序）
G3 E2E 引用：问知识类问题 → 回答带 postdetail 链接
G4 拒答：明确问不存在的内容 → 回答表示没找到（宽松断言）
"""
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8100"

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")
from app.api.deps import mint_token

H = {"Authorization": f"Bearer {mint_token('i6-user', 'user')}",
     "Content-Type": "application/json"}


def g1_sync():
    from app.win_compat import setup_event_loop

    setup_event_loop()
    from app.rag.sync import sync_recent

    n = asyncio.run(sync_recent(50))
    assert n >= 1, f"synced {n}"
    print(f"[G1] sync OK: {n} documents")


async def _search(query: str):
    from dotenv import load_dotenv

    from app.config import get_config
    from app.rag import store

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    await store.init_rag(get_config())
    try:
        return await store.search(query, 5)
    finally:
        await store.close_rag()


def g2_search():
    hits = asyncio.run(_search("大学宿舍生活 相关的帖子"))
    assert hits, "no hits"
    assert hits[0]["score"] > hits[-1]["score"], "scores must be ordered"
    print(f"[G2] search OK: top score={hits[0]['score']} title={hits[0]['title'][:30]}")
    return hits


def _sse(body: dict) -> list[tuple[str, dict]]:
    req = urllib.request.Request(BASE + "/task/sse", data=json.dumps(body).encode("utf-8"),
                                 headers=H, method="POST")
    events = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                events.append((event, json.loads(line[6:])))
                if event in ("done", "error", "suspended"):
                    break
    return events


def _last_text(events) -> str:
    return "".join(d.get("text", "") for ev, d in events if ev == "token")


def g3_citation():
    hits = g2_search()
    target_title = hits[0]["title"][:10]
    # LLM 非确定性：重试 3 次
    ok = False
    reply = ""
    for attempt in range(3):
        events = _sse({
            "message": f"知识库里有没有关于「{target_title}」的帖子？用 kb_search 查一下并给我引用链接",
            "thread_id": f"t-i6-g3-{attempt}",
        })
        ts = [d for ev, d in events if ev == "tool_start" and d["name"] == "kb_search"]
        if not ts:
            print(f"  [g3] attempt {attempt}: kb_search not called, retry")
            continue
        reply = _last_text(events)
        if "postdetail" in reply:
            ok = True
            break
    assert ok, f"no citation in reply: {reply[:200]}"
    print(f"[G3] citation OK: {reply[:80]}")


def g4_refusal():
    ok = False
    reply = ""
    for attempt in range(3):
        events = _sse({
            "message": "用 kb_search 查一下「量子纠缠在食堂打饭中的应用」",
            "thread_id": f"t-i6-g4-{attempt}",
        })
        reply = _last_text(events)
        if any(k in reply for k in ("没有找到", "未找到", "没有相关", "查不到", "无法找到")):
            ok = True
            break
    assert ok, f"expected refusal, got: {reply[:200]}"
    print(f"[G4] refusal OK: {reply[:60]}")


def g4_degraded():
    """降级模式：kb_search 不可用时必须明说，不得编造。"""
    ok = False
    reply = ""
    for attempt in range(3):
        events = _sse({
            "message": "用 kb_search 查一下「量子纠缠在食堂打饭中的应用」",
            "thread_id": f"t-i6-g4d-{attempt}",
        })
        reply = _last_text(events)
        if any(k in reply for k in ("暂不可用", "没有找到", "未找到", "没有相关", "查不到")):
            ok = True
            break
    assert ok, f"expected degraded refusal, got: {reply[:200]}"
    print(f"[G4-degraded] refusal OK: {reply[:60]}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--degraded":
        g4_degraded()
        print("I6 acceptance (degraded): PASS")
    else:
        g1_sync()
        g2_search()
        g3_citation()
        g4_refusal()
        print("I6 acceptance: ALL PASS")
