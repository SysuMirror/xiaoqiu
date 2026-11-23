"""进程内运行时状态：main 与 compat 共享，避免循环导入。"""
from __future__ import annotations

import asyncio
import os

state: dict = {}

# 断连宽限：thread_id -> 定时取消任务；run_id -> producer task
grace_tasks: dict[str, asyncio.Task] = {}
producers: dict[str, asyncio.Task] = {}
# 同 thread 取消旧 run（2026-10-11 切界面重发实录：thread_id -> (run_id, producer)）
thread_runs: dict[str, tuple[str, asyncio.Task]] = {}


def register_run(thread_id: str, run_id: str, producer: asyncio.Task) -> None:
    """run 注册：run_id 索引（宽限取消用）+ thread 归属（新请求取消旧 run 用）。"""
    producers[run_id] = producer
    thread_runs[thread_id] = (run_id, producer)


def unregister_run(thread_id: str, run_id: str) -> None:
    """run 结束清理（produce finally 调；同 thread 已被新 run 顶替时不误删）。"""
    producers.pop(run_id, None)
    if thread_runs.get(thread_id, (None, None))[0] == run_id:
        thread_runs.pop(thread_id, None)


def cancel_thread_run(thread_id: str) -> str | None:
    """同 thread 新请求：立即取消仍在跑的旧 run，返回被取消的 run_id。

    此前新请求只 cancel_grace（停定时器），旧 producer 永生——前端切界面重发后
    同 thread 两 run 并发写 checkpoint、token 双烧（2026-10-11 实录 22.4 万 token
    重叠 run）。runs 状态由 produce 的 CancelledError 处理标 cancelled。
    """
    cancel_grace(thread_id)
    entry = thread_runs.get(thread_id)
    if not entry:
        return None
    run_id, producer = entry
    if producer.done():
        unregister_run(thread_id, run_id)
        return None
    producer.cancel()
    return run_id

# Skein 调用留痕：SKEIN_ASSISTANT_KEY 未设时零开销（本地开发不回报）
def trace_patch(config: dict, thread_id: str, tags: list[str] | None = None) -> dict:
    """给 LangGraph 调用 config 挂 Skein 留痕回调。

    - 会话：thread_id；用户：configurable.user_id；来源：tags[0]（ops/chat）
    - SKEIN_ASSISTANT_KEY 未设时原样返回（本地开发零开销）
    """
    key = os.environ.get("SKEIN_ASSISTANT_KEY", "")
    if not key:
        return config
    base = os.environ.get("SKEIN_CONTROL_URL", "http://172.17.0.1:7800")
    from app.observability.skein_trace import SkeinTraceCallback

    user_id = (config.get("configurable") or {}).get("user_id")
    source = tags[0] if tags else "chat"
    handler = SkeinTraceCallback(base, key, thread_id, str(user_id or ""), source)
    config.setdefault("callbacks", [])
    if isinstance(config["callbacks"], list):
        config["callbacks"].append(handler)
    return config


def cancel_grace(thread_id: str) -> None:
    task = grace_tasks.pop(thread_id, None)
    if task:
        task.cancel()


def get_graph(role: str | None = None):
    from app.config import get_config

    graphs = state.get("graphs") or {}
    return graphs.get(role or get_config().default_role)


def memory_manager():
    return state.get("memory_manager")


def schedule_grace_cancel(thread_id: str, run_id: str, grace: float) -> None:
    """断连宽限：到期未重连则取消 run。"""
    import logging

    cancel_grace(thread_id)

    async def _later():
        from app.memory import runs

        await asyncio.sleep(grace)
        grace_tasks.pop(thread_id, None)
        producer = producers.pop(run_id, None)
        if producer and not producer.done():
            producer.cancel()
            await runs.finish(run_id, "cancelled", error="disconnect_grace_expired")
            logging.getLogger("sse-agent").info("run %s cancelled after disconnect grace", run_id)

    grace_tasks[thread_id] = asyncio.create_task(_later())
