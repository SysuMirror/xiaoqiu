"""i51 验收：同 thread 新请求取消旧 run（C 项，2026-10-11 切界面重发实录）。

事故形态：前端切界面重发 → 旧 run 仍在跑（fetch 不 abort / 宽限期 producer
永生）→ 同 thread 两 run 并发写 checkpoint、token 双烧（22.4 万 token 重叠 run）。

T1 注册/取消语义：cancel_thread_run 取消活旧 producer 并返回 run_id；
旧已结束则返回 None 并清理；unregister 不误删新 run 的归属。
T2 真取消：注册的 asyncio.Task 被真取消（CancelledError 语义）。
T3 宽限联动：cancel_thread_run 同时清掉该 thread 的断连宽限定时器。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime import (
    cancel_grace,
    cancel_thread_run,
    grace_tasks,
    producers,
    register_run,
    schedule_grace_cancel,
    thread_runs,
    unregister_run,
)


async def t1_registry():
    async def forever():
        await asyncio.sleep(3600)

    old = asyncio.create_task(forever())
    register_run("th1", "run-old", old)
    assert producers["run-old"] is old
    assert thread_runs["th1"] == ("run-old", old)

    # 活旧 run → 取消并返回
    got = cancel_thread_run("th1")
    assert got == "run-old", got
    await asyncio.sleep(0)  # 让取消生效
    assert old.cancelled()

    # 已结束 → 返回 None 并清理登记
    got2 = cancel_thread_run("th1")
    assert got2 is None
    assert "th1" not in thread_runs and "run-old" not in producers

    # unregister 不误删新 run 归属（旧 run 迟到的 finally）
    new = asyncio.create_task(forever())
    register_run("th1", "run-new", new)
    unregister_run("th1", "run-old")
    assert thread_runs["th1"] == ("run-new", new)
    unregister_run("th1", "run-new")
    new.cancel()
    assert cancel_thread_run("th1") is None  # 未注册 thread
    print("[T1] 注册/取消语义 OK（取消活旧 run / 已终清删 / 不误删新 run）")


async def t2_real_cancel():
    cancelled_flag = {"hit": False}

    async def work():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled_flag["hit"] = True  # produce 的 CancelledError handler 等价物
            raise

    t = asyncio.create_task(work())
    register_run("th2", "run-2", t)
    await asyncio.sleep(0)
    assert cancel_thread_run("th2") == "run-2"
    try:
        await t
        raise AssertionError("应被取消")
    except asyncio.CancelledError:
        pass
    assert cancelled_flag["hit"]
    print("[T2] 真取消 OK（CancelledError 送达 task，handler 有机会标 cancelled）")


async def t3_grace_interplay():
    async def forever():
        await asyncio.sleep(3600)

    t = asyncio.create_task(forever())
    register_run("th3", "run-3", t)
    schedule_grace_cancel("th3", "run-3", 0.05)
    assert "th3" in grace_tasks
    cancel_thread_run("th3")
    assert "th3" not in grace_tasks  # 宽限定时器一并清掉，不再二次触发
    await asyncio.sleep(0.1)
    t.cancel()
    print("[T3] 宽限联动 OK（取消旧 run 同时清宽限定时器）")


async def main():
    await t1_registry()
    await t2_real_cancel()
    await t3_grace_interplay()
    print("i51 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
