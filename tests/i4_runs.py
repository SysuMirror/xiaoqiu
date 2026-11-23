"""I4 验收：run 生命周期。

R1 正常完成：SSE 跑完 → runs 表 succeeded
R2 断连宽限：读到一部分就断开 → run 继续完成 → /task/result 拉到完整回复
R3 并发上限：同用户并发 run 超限 → 429
R4 孤儿对账：插入 running 记录 → 重启服务 → 变 failed(server_restart)
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8100"

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")
from app.api.deps import mint_token

H = {"Authorization": f"Bearer {mint_token('i4-user', 'user')}",
     "Content-Type": "application/json"}


async def _db():
    import asyncpg

    return await asyncpg.create_pool(os.environ["AGENT_PG_DSN"])


def sse_read_some(body: dict, read_bytes: int = 200, stop: threading.Event | None = None):
    """打开 SSE 读少量数据后断开（模拟客户端断连）。"""
    req = urllib.request.Request(BASE + "/task/sse", data=json.dumps(body).encode("utf-8"),
                                 headers=H, method="POST")
    data = b""
    with urllib.request.urlopen(req, timeout=240) as resp:
        for raw in resp:
            data += raw
            if stop is not None and stop.is_set():
                break
            if len(data) >= read_bytes:
                break  # 主动断开
    return data


def r1_normal_complete():
    body = {"message": "只回复两个字：好的", "thread_id": "t-i4-r1"}
    with urllib.request.urlopen(urllib.request.Request(
            BASE + "/task/sse", data=json.dumps(body).encode("utf-8"),
            headers=H, method="POST"), timeout=240) as resp:
        for _ in resp:
            pass
    asyncio.run(_check_status("t-i4-r1", {"succeeded"}))
    print("[R1] normal run -> succeeded OK")


async def _check_status(thread_id: str, allowed: set[str]) -> str:
    pool = await _db()
    try:
        row = await pool.fetchrow("SELECT status FROM runs WHERE thread_id=$1"
                                  " ORDER BY created_at DESC LIMIT 1", thread_id)
        assert row and row["status"] in allowed, f"thread={thread_id} status={row}"
        return row["status"]
    finally:
        await pool.close()


def r2_disconnect_then_result():
    body = {"message": "论坛最新的3条帖子是什么？列一下标题", "thread_id": "t-i4-r2"}
    # 开子线程读一点就断开
    stop = threading.Event()

    def reader():
        sse_read_some(body, read_bytes=300, stop=stop)

    t = threading.Thread(target=reader)
    t.start()
    import time
    time.sleep(1.5)
    stop.set()
    t.join(timeout=30)

    # run 应继续跑完（宽限 90s 内），随后 result 可拉
    import time
    deadline = time.time() + 60
    status = "running"
    while time.time() < deadline:
        status = asyncio.run(_check_status("t-i4-r2", {"succeeded", "failed", "running", "cancelled"}))
        if status == "succeeded":
            break
        time.sleep(3)
    assert status == "succeeded", f"run did not complete after disconnect: {status}"

    # /task/result 拉取完整回复
    url = f"{BASE}/task/result?thread_id=t-i4-r2"
    req = urllib.request.Request(url, headers=H)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data["status"] == "succeeded" and data["reply"], f"result: {data}"
    assert "postdetail" in data["reply"] or "帖子" in data["reply"], f"reply lacks content: {data['reply'][:100]}"
    print(f"[R2] disconnect grace + result OK: reply[:40]={data['reply'][:40]}")


def r3_concurrency_limit():
    # 临时把并发上限调成 1：需要改 yaml 重启太重，直接开两个慢消费连接再发第三个
    # 简化：并发检查读 runs 表 running 计数。R2 的断连 run 已 succeeded。
    # 造 2 条 running 记录模拟
    async def _insert():
        pool = await _db()
        await pool.execute("INSERT INTO runs (run_id, thread_id, user_id, status)"
                           " VALUES ('fake1','t-fake1','i4-user','running')")
        await pool.execute("INSERT INTO runs (run_id, thread_id, user_id, status)"
                           " VALUES ('fake2','t-fake2','i4-user','running')")
        await pool.close()

    asyncio.run(_insert())
    body = {"message": "hi", "thread_id": "t-i4-r3"}
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/task/sse", data=json.dumps(body).encode("utf-8"),
            headers=H, method="POST"), timeout=30)
        raise AssertionError("expected 429")
    except urllib.error.HTTPError as e:
        assert e.code == 429, f"expected 429, got {e.code}"
    print("[R3] concurrency limit OK (429)")

    async def _cleanup():
        pool = await _db()
        await pool.execute("DELETE FROM runs WHERE run_id IN ('fake1','fake2')")
        await pool.close()

    asyncio.run(_cleanup())


def r4_orphan_reconcile():
    subprocess.run(["bash", str(Path(__file__).resolve().parent.parent / "scripts" / "restart_server.sh")],
                   capture_output=True, timeout=120)
    asyncio.run(_check_status("t-fake1", {"failed"}))  # fake1 已被清理，改为验证机制本身
    print("[R4] orphan reconcile OK")


def r4_orphan_reconcile_proper():
    async def _insert():
        pool = await _db()
        await pool.execute("INSERT INTO runs (run_id, thread_id, user_id, status)"
                           " VALUES ('orphan1','t-orphan','i4-user','running')"
                           " ON CONFLICT (run_id) DO UPDATE SET status='running', finished_at=NULL")
        await pool.close()

    asyncio.run(_insert())
    subprocess.run(["bash", str(Path(__file__).resolve().parent.parent / "scripts" / "restart_server.sh")],
                   capture_output=True, timeout=120)
    asyncio.run(_check_status("t-orphan", {"failed"}))
    print("[R4] orphan reconcile OK (running -> failed/server_restart)")


if __name__ == "__main__":
    r1_normal_complete()
    r2_disconnect_then_result()
    r3_concurrency_limit()
    r4_orphan_reconcile_proper()
    print("\nI4 acceptance: ALL PASS")
