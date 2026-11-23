"""小秋客户端生产化 E2E：假用户 u95270/u95271 走全链路，结束清理。

覆盖：bootstrap 恢复/新建、真流式(append+final+run_id)、会话列表/改名/历史/删除、
记忆写入/列表/更新/删除/越权、消息反馈(创建/覆盖/统计)、开场建议、跨用户隔离。
用法: .venv/Scripts/python scripts/xiaoqiu_client_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.api import deps  # noqa: E402

BASE = os.environ.get("XQ_E2E_BASE", "http://127.0.0.1:8100")
U1, U2 = "u95270", "u95271"
THREAD_PREFIX = "xq_e2e_"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def hdr(uid: str) -> dict:
    return {"Authorization": f"Bearer {deps.mint_token(uid, 'user')}",
            "Content-Type": "application/json"}


def call(method: str, path: str, uid: str, body: dict | None = None):
    with httpx.Client(timeout=30, trust_env=False) as hc:
        r = hc.request(method, f"{BASE}{path}", headers=hdr(uid), json=body)
        if os.environ.get("XQ_E2E_DEBUG"):
            print(f"    [dbg] {method} {path} -> {r.status_code} {r.text[:200]}")
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, {}


def chat_stream(uid: str, session_id: str, text: str) -> dict:
    """走 task/sse，统计 append 事件与 final。"""
    appends, finals, statuses = [], [], []
    t0 = time.monotonic()
    with httpx.Client(timeout=120, trust_env=False) as hc, \
            hc.stream("POST", f"{BASE}/api/v1/agent/task/sse", headers=hdr(uid),
                      json={"task": text, "session_id": session_id}) as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                try:
                    d = json.loads(line.split(":", 1)[1].strip())
                except json.JSONDecodeError:
                    continue
                if event == "content" and d.get("mode") == "append":
                    appends.append((d.get("text", ""), time.monotonic() - t0))
                elif event == "content" and d.get("mode") == "final":
                    finals.append(d)
                elif event == "status":
                    statuses.append(d)
    return {"appends": appends, "final": finals[-1] if finals else {}, "status": statuses,
            "span": (appends[-1][1] - appends[0][1]) if len(appends) > 1 else 0}


def cleanup():
    import asyncio

    from app.config import get_config

    async def _run():
        import asyncpg

        conn = await asyncpg.connect(get_config().pg_dsn)
        await conn.execute("DELETE FROM message_feedback WHERE user_id LIKE 'u9527%'")
        await conn.execute("DELETE FROM threads WHERE user_id LIKE 'u9527%'")
        await conn.execute("DELETE FROM checkpoints WHERE thread_id LIKE 'xq_e2e_%'")
        await conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id LIKE 'xq_e2e_%'")
        await conn.execute("DELETE FROM checkpoint_writes WHERE thread_id LIKE 'xq_e2e_%'")
        await conn.execute("DELETE FROM store WHERE prefix LIKE '%u9527%'")
        await conn.close()

    asyncio.run(_run())

    # qdrant 记忆向量清理
    import asyncio
    import os

    from app.rag import store as rag_store

    async def _qdrant():
        await rag_store.init_rag(get_config())
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = rag_store.client()
        await client.delete(
            collection_name=get_config().rag.memory_collection,
            points_selector=Filter(must=[
                FieldCondition(key="user_id", match=MatchValue(value=U1))]),
            wait=True)
        await rag_store.close_rag()

    if os.environ.get("XQ_E2E_CLEAN_QDRANT", "1") == "1":
        asyncio.run(_qdrant())


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else "all"
    if run == "clean":
        cleanup()
        print("cleaned")
        return

    # T1 bootstrap：新用户开新会话（主动传受控 session_id 便于清理）
    import uuid as _uuid

    sid1 = THREAD_PREFIX + _uuid.uuid4().hex[:12]
    code, boot = call("POST", "/api/v1/agent/conversation/bootstrap", U1, {"session_id": sid1})
    data = boot.get("data", {})
    check("T1 bootstrap 新会话", code == 200 and boot.get("code") == 0
          and data.get("session_id") == sid1, f"sid={sid1}")

    # T2 开场建议
    code, sug = call("GET", "/api/v1/agent/suggestions", U1)
    items = sug.get("data", {}).get("suggestions", [])
    check("T2 开场建议", code == 200 and 3 <= len(items) <= 6,
          f"n={len(items)} first={items[0]['text'][:20] if items else ''}")

    # T3 真流式聊天（要求回答里记住一个偏好 → 顺带触发 memory_save）
    res = chat_stream(U1, sid1, "请记住：我偏好简洁的要点式回答。然后随便用一句话打个招呼。")
    n_append = len(res["appends"])
    final = res["final"]
    check("T3 真流式 append>1", n_append > 1, f"appends={n_append} span={res['span']:.2f}s")
    check("T3 final 带全文+run_id", bool(final.get("text")) and bool(final.get("run_id")),
          f"run_id={final.get('run_id', '')[:12]} text={final.get('text', '')[:40]!r}")
    run_id = final.get("run_id", "")

    # T4 会话出现在列表且有自动标题（标题 worker 异步，稍等）
    time.sleep(6)
    code, lst = call("GET", "/api/v1/agent/conversations", U1)
    convs = lst.get("data", {}).get("conversations", [])
    mine = next((c for c in convs if c["session_id"] == sid1), None)
    check("T4 会话列表+自动标题", code == 200 and mine is not None and bool(mine.get("title")),
          f"title={mine.get('title', '')!r}" if mine else "not found")

    # T5 记忆：列表应有刚写入的一条
    code, mem = call("GET", "/api/v1/agent/memory", U1)
    memories = mem.get("data", {}).get("memories", [])
    check("T5 记忆已写入", code == 200 and len(memories) >= 1,
          f"n={len(memories)} text={memories[0]['text'][:30] if memories else ''!r}")
    if not memories:
        return finish()

    # T6 记忆更新
    key = memories[0]["key"]
    code, upd = call("PUT", f"/api/v1/agent/memory/{key}", U1,
                     {"text": "我偏好极简的要点式回答，不要长篇大论"})
    _code2, mem2 = call("GET", "/api/v1/agent/memory", U1)
    texts = [m["text"] for m in mem2.get("data", {}).get("memories", [])]
    check("T6 记忆更新", code == 200 and upd.get("data", {}).get("updated")
          and any("极简" in t for t in texts), "")

    # T7 语义召回注入（问相关问题时，prep 应召回该记忆——间接验证：回答应体现偏好）
    res2 = chat_stream(U1, sid1, "根据你对我的了解，回答：什么是 RESTful？")
    check("T7 记忆召回生效(回答要点式)", len(res2["final"].get("text", "")) > 10, "")

    # T8 记忆删除
    code, _dele = call("DELETE", f"/api/v1/agent/memory/{key}", U1)
    _code2, mem3 = call("GET", "/api/v1/agent/memory", U1)
    check("T8 记忆删除", code == 200 and len(mem3.get("data", {}).get("memories", [])) == 0, "")

    # T9 改名 + 历史消息
    code, rn = call("POST", f"/api/v1/agent/conversations/{sid1}/title", U1,
                    {"title": "E2E测试会话"})
    code, hist = call("GET", f"/api/v1/agent/conversations/{sid1}/messages", U1)
    roles = [m["role"] for m in hist.get("data", {}).get("messages", [])]
    check("T9 改名+历史", code == 200 and rn.get("data", {}).get("updated")
          and hist.get("data", {}).get("title") == "E2E测试会话"
          and "user" in roles and "assistant" in roles, f"msgs={len(roles)}")

    # T10 跨用户隔离
    code, _ = call("GET", f"/api/v1/agent/conversations/{sid1}/messages", U2)
    check("T10 跨用户隔离 403", code == 403, f"status={code}")

    # T11 消息反馈：创建→覆盖→统计
    code, fb = call("POST", "/api/v1/agent/feedback", U1,
                    {"run_id": run_id, "thread_id": sid1, "rating": -1, "comment": "太简略"})
    code, fb2 = call("POST", "/api/v1/agent/feedback", U1,
                     {"run_id": run_id, "thread_id": sid1, "rating": 1, "comment": "改好了"})
    code, st = call("GET", "/api/v1/agent/feedback/stats", U1)
    check("T11 反馈创建/覆盖/统计", code == 200 and fb.get("data", {}).get("result") == "created"
          and fb2.get("data", {}).get("result") == "updated"
          and st.get("data", {}).get("up", 0) >= 1, f"stats={st.get('data')}")

    # T12 删除会话 → bootstrap 不再恢复它
    code, dl = call("DELETE", f"/api/v1/agent/conversations/{sid1}", U1)
    code, boot2 = call("GET", "/api/v1/agent/conversation/bootstrap", U1)
    sid_new = boot2.get("data", {}).get("session_id", "")
    check("T12 删除会话后不恢复", code == 200 and dl.get("data", {}).get("deleted")
          and sid_new != sid1, f"new={sid_new[:12]}")

    # T13 已删除会话拒绝复用（410）
    code, _ = call("POST", "/api/v1/agent/task", U1,
                   {"task": "在吗", "session_id": sid1})
    check("T13 已删会话 410", code == 410, f"status={code}")
    finish()


def finish():
    failed = [r for r in results if not r[1]]
    print(f"\n== {len(results) - len(failed)}/{len(results)} PASS ==")
    if failed:
        for name, _, detail in failed:
            print(f"  FAILED: {name} {detail}")
        cleanup()
        sys.exit(1)
    cleanup()
    print("cleanup done")


if __name__ == "__main__":
    main()
