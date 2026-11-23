"""i49 验收：PG 池换血韧性（根治 2026-10-11 PG 重启连环 50000）。

事故形态：PG 重启杀掉 chat 进程内连接池的旧连接，池不自愈，之后所有
对话请求拿到死连接报 50000，探针全绿，只能人肉重建 pod。
（app/db.py 三层治理：command_timeout 上界 + 换血重试 + deep_check 深检）

T1 复刻事故：pg_terminate_backend 杀掉池内全部后端连接后，池便利方法与
   acquire 代理的单语句调用必须自愈，不得报错。
T2 换血路径单测：stub 池首次调用抛连接类异常，断言 expire_connections
   被触发且第二次（新连接）重放成功——覆盖 is_closed=False 的半开形态。
T3 deep_check：杀连接后深检恢复 ok（/health/deep 的自愈语义）。

运行前提：AGENT_PG_DSN 可达（本地隧道 scripts/dev_tunnel.sh 或同网环境）。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import asyncpg

from app import db


async def t1_kill_backends():
    """复刻 10-11 事故：杀光池内后端连接，后续请求必须自愈。"""
    dsn = os.environ["AGENT_PG_DSN"]
    pool = await db.create_pool(dsn, name="i49-test", min_size=2, max_size=2)
    try:
        # 预热并摸出池内全部后端 pid（min=max=2，最多两根）
        pids = set()
        for _ in range(6):
            pids.add(await pool.fetchval("SELECT pg_backend_pid()"))
        assert pids, "未摸到任何后端连接"

        admin = await asyncpg.connect(dsn)
        try:
            for pid in pids:
                await admin.execute("SELECT pg_terminate_backend($1)", int(pid))
        finally:
            await admin.close()

        # 事故后流量：连续单语句必须全成功（首摸坏连接自动换血）
        for _ in range(10):
            assert await pool.fetchval("SELECT 42") == 42
        # acquire 代理路径同样自愈
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 43") == 43
        print(f"[T1] 杀 {len(pids)} 根后端连接后池自愈 OK（便利方法 + acquire 代理）")
    finally:
        await pool.close()


class _StaleOncePool:
    """stub：首次调用抛连接类异常（模拟半开连接），expire 后新连接成功。"""

    def __init__(self):
        self.calls = 0
        self.expired = 0

    async def execute(self, sql, *args, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise asyncpg.exceptions.ConnectionDoesNotExistError()
        return "OK"

    async def expire_connections(self):
        self.expired += 1


async def t2_reap_retry():
    """stub 单测：连接类异常 → 整池过期 → 新连接重放一次成功。"""
    stub = _StaleOncePool()
    wrapper = db.Pool("stub", stub)
    assert await wrapper.execute("SELECT 1") == "OK"
    assert stub.calls == 2 and stub.expired == 1, (stub.calls, stub.expired)

    # 非连接类异常不换血不重试（业务错误穿透）
    class _BizErrPool:
        async def execute(self, sql, *a, timeout=None):
            raise RuntimeError("boom")

    try:
        await db.Pool("stub2", _BizErrPool()).execute("SELECT 1")
        raise AssertionError("业务异常应穿透")
    except RuntimeError:
        pass
    print("[T2] 换血重试路径 OK（连接类异常 expire+重放；业务异常穿透）")


async def t3_deep_check():
    """/health/deep 语义：杀连接后 deep_check 恢复 ok。"""
    dsn = os.environ["AGENT_PG_DSN"]
    pool = await db.create_pool(dsn, name="i49-deep", min_size=1, max_size=1)
    try:
        assert (await db.deep_check()).get("i49-deep") == "ok"
        pid = await pool.fetchval("SELECT pg_backend_pid()")
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute("SELECT pg_terminate_backend($1)", int(pid))
        finally:
            await admin.close()
        # 深检自带一次换血：要么这次调用直接自愈，要么下一轮 ok
        for _ in range(2):
            if (await db.deep_check()).get("i49-deep") == "ok":
                print("[T3] deep_check 杀连接后恢复 ok OK")
                return
        raise AssertionError("deep_check 未恢复")
    finally:
        await pool.close()


async def main():
    await t1_kill_backends()
    await t2_reap_retry()
    await t3_deep_check()
    print("i49 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
