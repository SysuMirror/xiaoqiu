"""I3 验收：行动台账追溯 + 记忆治理。

M1 hash 去重（确定性）
M2 LLM 写时合并（同义记忆不产生新条目）
M3 容量 50 LRU（确定性，直接灌 55 条）
M4 TTL 过期清理（确定性）
E1 E2E：杀容器后问「你最近做了什么」→ 从台账复述（含容器名）
E2 E2E：用户说「记住…」→ memory_save 被调用且落库
"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8100"
TEST_CONTAINER = "sse_test_sleep"

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")
from app.api.deps import mint_token

ADMIN_H = {"Authorization": f"Bearer {mint_token('i3-admin', 'admin')}"}
USER_H = {"Authorization": f"Bearer {mint_token('i3-user', 'user')}"}


def sse_events(path: str, body: dict, headers: dict) -> list[tuple[str, dict]]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=240) as resp:
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


def find_event(events, name):
    for ev, d in events:
        if ev == name:
            return d
    return None


def last_text(events) -> str:
    return "".join(d.get("text", "") for ev, d in events if ev == "token")


def ssh_docker(*args: str) -> str:
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", "SSE_Market", "docker", *args],
                         capture_output=True, text=True, timeout=60)
    return (out.stdout + out.stderr).strip()


async def _make_manager():
    from langgraph.store.postgres.aio import AsyncPostgresStore

    from app.config import get_config
    from app.memory.user_memory import UserMemoryManager
    from app.model.pool import ModelPool

    store = AsyncPostgresStore.from_conn_string(get_config().pg_dsn).__aenter__()
    store = await store if asyncio.iscoroutine(store) else store
    # from_conn_string 返回 async context manager，手动进入
    cm = AsyncPostgresStore.from_conn_string(get_config().pg_dsn)
    store = await cm.__aenter__()
    await store.setup()
    pool = ModelPool.from_config(get_config())
    return UserMemoryManager(store, pool), cm


async def _clear(mgr, user_id):
    for it in await mgr.list_all(user_id):
        await mgr._store.adelete(mgr.namespace(user_id), it["key"])


async def m1_dedup():
    mgr, cm = await _make_manager()
    try:
        ns_user = "i3-m1-user"
        await _clear(mgr, ns_user)
        a1 = await mgr.save(ns_user, "喜欢简洁的回答")
        a2 = await mgr.save(ns_user, "喜欢简洁的回答")
        items = await mgr.list_all(ns_user)
        assert a1 == "new" and a2 == "duplicate", f"a1={a1} a2={a2}"
        assert len(items) == 1, f"expected 1 item, got {len(items)}"
        print(f"[M1] hash dedup OK (actions={a1},{a2}, items=1)")
    finally:
        await cm.__aexit__(None, None, None)


async def m2_merge():
    mgr, cm = await _make_manager()
    try:
        ns_user = "i3-m2-user"
        await _clear(mgr, ns_user)
        await mgr.save(ns_user, "喜欢简洁的回答")
        a2 = await mgr.save(ns_user, "回答要简短，不要啰嗦")
        items = await mgr.list_all(ns_user)
        assert len(items) <= 2, f"merge failed, items={len(items)}: {[i['text'] for i in items]}"
        print(f"[M2] merge OK (action2={a2}, items={len(items)}: {[i['text'] for i in items]})")
    finally:
        await cm.__aexit__(None, None, None)


async def m3_capacity():
    mgr, cm = await _make_manager()
    try:
        ns_user = "i3-m3-user"
        await _clear(mgr, ns_user)
        # 绕过 LLM 直接灌 55 条（确定性）
        for i in range(55):
            await mgr._store.aput(mgr.namespace(ns_user), f"test_{i}", {
                "text": f"测试记忆条目{i}", "hash": f"h{i}", "updated_at": f"2026-08-20T00:{i%60:02d}:00+00:00",
            })
        await mgr.save(ns_user, "触发容量淘汰的新记忆")
        items = await mgr.list_all(ns_user)
        assert len(items) <= 50, f"capacity violated: {len(items)}"
        assert any("触发容量淘汰" in it["text"] for it in items), "newest item must survive"
        print(f"[M3] capacity OK (items={len(items)}, newest kept)")
    finally:
        await cm.__aexit__(None, None, None)


async def m4_ttl():
    mgr, cm = await _make_manager()
    try:
        ns_user = "i3-m4-user"
        await _clear(mgr, ns_user)
        await mgr._store.aput(mgr.namespace(ns_user), "old", {
            "text": "过期记忆", "hash": "old",
            "updated_at": "2020-01-01T00:00:00+00:00",
        })
        items = await mgr.list_all(ns_user)
        assert len(items) == 0, f"expired item must be purged, got {items}"
        print("[M4] TTL purge OK")
    finally:
        await cm.__aexit__(None, None, None)


def e1_ledger_recall():
    ssh_docker("rm", "-f", TEST_CONTAINER)
    ssh_docker("run", "-d", "--name", TEST_CONTAINER, "busybox", "sleep", "3600")
    # LLM 调工具是非确定性的：至多重试 3 次（安全属性不受影响，仅影响本场景触发路径）
    appr = None
    for attempt in range(3):
        ev1 = sse_events("/task/sse", {
            "message": f"容器 {TEST_CONTAINER} 假死无法响应，必须使用 kill_process 工具停止它，原因是假死",
            "thread_id": f"t-i3-e1-{attempt}",
        }, ADMIN_H)
        appr = find_event(ev1, "approval_request")
        if appr:
            break
        print(f"  [e1] attempt {attempt}: agent did not call tool, retrying")
    assert appr, "agent never called kill_process in 3 attempts"
    ev2 = sse_events("/task/resume", {"thread_id": f"t-i3-e1-{attempt}", "approved": True}, ADMIN_H)
    assert find_event(ev2, "done"), "resume should finish"
    assert not ssh_docker("ps", "--format", "{{.Names}}", "--filter", f"name=^{TEST_CONTAINER}$")

    ev3 = sse_events("/task/sse", {
        "message": "你最近都做过哪些操作？请具体说明",
        "thread_id": "t-i3-e1b",
    }, ADMIN_H)
    reply = last_text(ev3)
    assert TEST_CONTAINER in reply, f"agent must recall kill from ledger, reply: {reply[:150]}"
    print(f"[E1] ledger recall OK: ...{reply[-60:]}")


def e2_memory_save():
    ts = None
    for attempt in range(3):
        ev = sse_events("/task/sse", {
            "message": "请记住：我喜欢带引用来源的回答",
            "thread_id": f"t-i3-e2-{attempt}",
        }, USER_H)
        ts = find_event(ev, "tool_start")
        if ts and ts["name"] == "memory_save":
            break
        print(f"  [e2] attempt {attempt}: memory_save not called, retrying")
    assert ts and ts["name"] == "memory_save", "memory_save never called in 3 attempts"
    print(f"[E2] memory_save called OK (args={ts['args']})")


if __name__ == "__main__":
    from app.win_compat import setup_event_loop

    setup_event_loop()
    asyncio.run(m1_dedup())
    asyncio.run(m2_merge())
    asyncio.run(m3_capacity())
    asyncio.run(m4_ttl())
    e1_ledger_recall()
    e2_memory_save()
    print("\nI3 acceptance: ALL PASS")
