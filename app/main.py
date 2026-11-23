"""SSE-Agent 服务入口。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")

from app.api import deps
from app.config import get_config
from app.graph.build import build_graph
from app.memory import ledger, runs, threads
from app.model.pool import LEDGER, AllEndpointsFailedError, ModelPool
from app.tools.forum.sql_templates import close_forum_pool, init_forum_pool
from app.win_compat import setup_event_loop

setup_event_loop()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sse-agent")

from app.runtime import (
    cancel_grace as _cancel_grace,
)
from app.runtime import (
    cancel_thread_run as _cancel_thread_run,
)
from app.runtime import (
    get_graph as _get_graph,
)
from app.runtime import (
    producers as _producers,
)
from app.runtime import (
    register_run as _register_run,
)
from app.runtime import (
    schedule_grace_cancel as _schedule_grace_cancel,
)
from app.runtime import (
    state as _state,
)
from app.runtime import (
    trace_patch as _trace_patch,
)
from app.runtime import (
    unregister_run as _unregister_run,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    from app.tools.notify import mail

    mail.setup_mail(mail.MailCfg(**cfg.mail.model_dump()))
    await init_forum_pool()
    await ledger.init_ledger(cfg.pg_dsn)
    from app.moderation import close_moderation, init_moderation

    pool = ModelPool.from_config(cfg, LEDGER)
    await init_moderation(pool, cfg.pg_dsn)
    from app.game import close_game, init_game

    await init_game(cfg.pg_dsn)
    await threads.init_threads(cfg.pg_dsn)
    await runs.init_runs(cfg.pg_dsn)
    from app.memory import feedback

    await feedback.init_feedback(cfg.pg_dsn)
    from app.tasks.retention import close_retention, init_retention

    await init_retention(cfg.pg_dsn)
    from app.api import openapi_service

    await openapi_service.init_openapi(cfg.pg_dsn)
    await runs.quarantine_orphans()  # 启动对账（XR-1）：遗留 running → resumable 可恢复
    from app.rag import store as rag_store

    try:
        await rag_store.init_rag(cfg)
    except Exception as _rag_err:  # noqa: BLE001 —— Qdrant 不可用时 chat 仍可启动
        log.warning("rag init degraded: %s", _rag_err)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    # checkpointer/store 专用池（psycopg_pool 断线自动重连）：此前 from_conn_string
    # 单连接用到进程死，空闲连接被 PG/网络掐掉后所有对话在首个 checkpoint 写入处
    # 全灭（2026-10-14 ops 面实录：起跑 2.5h 后连接死，重派连败两轮，deep check
    # 测不到——它只探 app 自有池，不探 saver/store 连接）。
    # 陈旧连接根治三件套（2026-10-25）：池连接经 frp 到集市云 PG，NAT/frp 会静默
    # 掐掉空闲连接——发放出来的死连接让首轮操作即炸（ops 面 610ms 秒断实录，
    # classify unknown 无法归因）。①TCP keepalive 保住 NAT 映射+快速识别死链；
    # ②check 发放时验活（SELECT 1，坏连接就地换新）；③max_lifetime 定期换血。
    from psycopg_pool import AsyncConnectionPool

    _ckpt_kwargs = {"autocommit": True, "prepare_threshold": 0,
                    "keepalives": 1, "keepalives_idle": 25, "keepalives_interval": 5,
                    "keepalives_count": 2}
    _pool_opts = {"check": AsyncConnectionPool.check_connection, "max_lifetime": 300}
    ckpt_pool = AsyncConnectionPool(cfg.pg_dsn, min_size=1, max_size=4,
                                    open=False, kwargs=_ckpt_kwargs, **_pool_opts)
    await ckpt_pool.open(wait=True, timeout=30)
    saver = AsyncPostgresSaver(ckpt_pool)
    await saver.setup()
    store_pool = AsyncConnectionPool(cfg.pg_dsn, min_size=1, max_size=2,
                                     open=False, kwargs=_ckpt_kwargs, **_pool_opts)
    await store_pool.open(wait=True, timeout=30)
    store = AsyncPostgresStore(store_pool)
    await store.setup()
    from app.memory.user_memory import UserMemoryManager

    mm = UserMemoryManager(store, pool)
    # 向量语义召回后端：复用 rag 的 qdrant 客户端与 bge-m3 embedding
    mm.setup_vectors(rag_store.embed, rag_store.client(), cfg.rag.memory_collection,
                     recall_k=cfg.rag.memory_recall_k,
                     min_score=cfg.rag.memory_recall_min_score)
    await mm.ensure_collection()
    _state["memory_manager"] = mm
    _state["graphs"] = {
        role: build_graph(pool, role, checkpointer=saver, store=store)
        for role in ("user", "admin")
    }
    _state["pool"] = pool
    if os.environ.get("XIAOQIU_LOOPS", "0") == "1":
        _start_scheduler(cfg)
    else:
        log.info("background loops off")
    monitor_task = None
    if os.environ.get("FRONTDESK_ENABLED") == "1":
        from app.frontdesk import run_monitor

        monitor_task = asyncio.create_task(run_monitor())
        log.info("frontdesk monitor on")
    log.info("agent ready (env=%s)", cfg.env)
    yield
    if monitor_task is not None:
        monitor_task.cancel()
    _scheduler.shutdown(wait=False) if _scheduler else None
    await ckpt_pool.close()
    await store_pool.close()
    await close_forum_pool()
    await close_moderation()

    await close_game()
    await ledger.close_ledger()

    await close_retention()
    from app.api import openapi_service

    await openapi_service.close_openapi()
    await threads.close_threads()
    await runs.close_runs()
    from app.memory import feedback as _feedback

    await _feedback.close_feedback()
    from app.rag import store as rag_store

    await rag_store.close_rag()
    log.info("shutdown complete")


app = FastAPI(title="SSE-Agent", lifespan=lifespan)

# 客户端生产化 API（会话中心 / 记忆管理 / 消息反馈 / 开场建议）
from app.api import client_api as _client_api

app.add_api_route("/api/v1/agent/conversations", _client_api.conversations_list, methods=["GET"])
app.add_api_route("/api/v1/agent/conversations/{session_id}/messages",
                  _client_api.conversation_messages, methods=["GET"])
app.add_api_route("/api/v1/agent/conversations/{session_id}/title",
                  _client_api.conversation_rename, methods=["POST"])
app.add_api_route("/api/v1/agent/conversations/{session_id}",
                  _client_api.conversation_delete, methods=["DELETE"])
app.add_api_route("/api/v1/agent/memory", _client_api.memory_list, methods=["GET"])
app.add_api_route("/api/v1/agent/memory/{key}", _client_api.memory_delete, methods=["DELETE"])
app.add_api_route("/api/v1/agent/memory/{key}", _client_api.memory_update, methods=["PUT"])
app.add_api_route("/api/v1/agent/feedback", _client_api.message_feedback, methods=["POST"])
app.add_api_route("/api/v1/agent/feedback/stats", _client_api.feedback_stats, methods=["GET"])
app.add_api_route("/api/v1/agent/feedback/list", _client_api.feedback_admin_list, methods=["GET"])
app.add_api_route("/api/v1/agent/suggestions", _client_api.suggestions, methods=["GET"])
app.add_api_route("/api/v1/agent/me/usage", _client_api.me_usage, methods=["GET"])
app.add_api_route("/api/v1/agent/me/export", _client_api.me_export, methods=["GET"])
app.add_api_route("/api/v1/agent/me/delete", _client_api.me_delete, methods=["POST"])
app.add_api_route("/api/v1/agent/conversations/{session_id}/export",
                  _client_api.conversation_export, methods=["GET"])
from app.api import openapi_service as _openapi

app.add_api_route("/api/v1/agent/open/kb_search", _openapi.open_kb_search, methods=["POST"])

# MCP 网关（PAT 鉴权，scope 分组工具面）——主站 ApiTokenPanel 帮助文案承诺的接入点
from app.api import mcp_service as _mcp_service

app.add_api_route("/api/v1/agent/mcp", _mcp_service.mcp_endpoint, methods=["POST"])

# 猜拳（赢了没奖励，输了封号 5 分钟）+ 封禁状态（前端淡悬浮窗轮询）
from app.game import ban_status as _game_ban_status
from app.game import play as _game_play
from app.game import stats as _game_stats

app.add_api_route("/api/v1/agent/game/rps", _game_play, methods=["POST"])
app.add_api_route("/api/v1/agent/game/rps/stats", _game_stats, methods=["GET"])
app.add_api_route("/api/v1/agent/me/ban", _game_ban_status, methods=["GET"])


@app.middleware("http")
async def request_logging(request: Request, call_next):
    import time as _t
    import uuid as _uuid

    rid = request.headers.get("X-Request-Id") or _uuid.uuid4().hex[:12]
    t0 = _t.monotonic()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("rid=%s %s %s failed", rid, request.method, request.url.path)
        raise
    resp.headers["X-Request-Id"] = rid
    if request.url.path not in ("/health/live", "/health/ready", "/health/deep", "/metrics"):
        log.info("rid=%s %s %s -> %s (%.0fms)", rid, request.method,
                 request.url.path, resp.status_code, (_t.monotonic() - t0) * 1000)
    return resp


@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus 抓取（仅内网：nginx 只路由 /api/v1/agent/，此端点不暴露公网）。"""

    from fastapi.responses import PlainTextResponse

    from app.observability import metrics

    pool = _state.get("pool")
    if pool is not None:
        metrics.gauge("agent_llm_calls_total_value", pool.ledger.calls)
        metrics.gauge("agent_llm_tokens_prompt", pool.ledger.prompt_tokens)
        metrics.gauge("agent_llm_tokens_completion", pool.ledger.completion_tokens)
        for name, st in pool.breaker_status().items():
            metrics.gauge("agent_llm_breaker_open", 1.0 if st == "open" else 0.0, {"endpoint": name})
    try:
        active = await runs.active_count_all()
        metrics.gauge("agent_runs_active", active)
    except Exception:  # noqa: BLE001
        pass
    return PlainTextResponse(metrics.expose(), media_type="text/plain; version=0.0.4")


_scheduler = None


def _start_scheduler(cfg) -> None:
    """APScheduler 进程内调度（chat 可选；retention + 日报 + RAG 对账）。"""
    global _scheduler
    from zoneinfo import ZoneInfo

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from app.api.internal import reconcile
    from app.tasks.daily_digest import run_daily_digest
    from app.tasks.retention import run_retention

    _scheduler = AsyncIOScheduler(timezone=ZoneInfo(cfg.timezone))
    _scheduler.add_job(
        run_daily_digest,
        CronTrigger(hour=cfg.scheduler.digest_hour, minute=cfg.scheduler.digest_minute),
        id="daily_digest",
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.add_job(reconcile, "interval", minutes=10, id="rag_reconcile",
                       max_instances=1, coalesce=True)

    async def _retention_job():
        try:
            await run_retention()
        except Exception:
            import logging as _lg

            _lg.getLogger(__name__).exception("retention job failed")

    _scheduler.add_job(
        _retention_job, "cron", hour=4, minute=17, id="retention",
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    _scheduler.start()
    log.info("scheduler started: daily_digest at %02d:%02d %s",
             cfg.scheduler.digest_hour, cfg.scheduler.digest_minute, cfg.timezone)


@app.post("/admin/jobs/digest/run")
async def admin_run_digest(request: Request):
    """手动触发日报（验收/补发用）。force=1 跳过当日幂等检查。"""
    identity = deps.verify_request(request)
    if identity.role != "admin":
        from fastapi.responses import JSONResponse as JR

        return JR(status_code=403, content={"error": "需要 admin 角色"})
    from app.tasks.daily_digest import run_daily_digest

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 —— 空请求体
        pass
    result = await run_daily_digest(force=bool(body.get("force")))
    return result


from app.api import compat
from app.api.internal import router as internal_router

app.include_router(internal_router)


@app.get("/api/v1/agent/conversation/bootstrap")
async def compat_bootstrap(request: Request):
    return await compat.bootstrap(request)


@app.post("/api/v1/agent/conversation/bootstrap")
async def compat_bootstrap_post(request: Request):
    return await compat.bootstrap(request)


@app.post("/api/v1/agent/conversation/close")
async def compat_close(request: Request):
    return await compat.close(request)


@app.post("/api/v1/agent/task")
async def compat_task(request: Request):
    return await compat.task_sync(request)


@app.post("/api/v1/agent/task/sse")
async def compat_task_sse(request: Request):
    return await compat.task_sse(request)


@app.post("/api/v1/agent/task/resume")
async def compat_resume(request: Request):
    return await compat.resume(request)


@app.get("/health/live")
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    pool = _state.get("pool")
    return JSONResponse(
        status_code=200 if _state.get("graphs") else 503,
        content={"status": "ok" if _state.get("graphs") else "starting",
                 "breakers": pool.breaker_status() if pool else {},
                 "chain": pool.chain_status() if pool else []},
    )


@app.get("/health/deep")
async def health_deep():
    """深检：真过全部已注册 PG 池跑 SELECT 1（每次调用自带一次换血重试）。

    供 k8s liveness 池死自愈——/health/live 不碰 DB，2026-10-11 PG 重启后
    池死透了探针仍全绿，只能人肉重建 pod。探针指到这里后：池坏 60s 内
    kubelet 自动重启（readiness 5s×6 / liveness 15s×4）。
    PG 整体不可用时本端点 503 → pod 摘流+重启，PG 恢复后自愈。
    """
    from app import db

    pools = await db.deep_check()
    ok = bool(pools) and all(v == "ok" for v in pools.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "fail", "pools": pools},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(graph, graph_input, thread_id: str, cfg_heartbeat: int,
                  ctx: dict | None = None, run_id: str | None = None):
    """生产者：图流事件入队；消费者：SSE 出口（含心跳）。

    SSE 是观察通道：客户端断开 → 进入断连宽限（run 继续跑），超时才真正取消。
    """
    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()
    state = {"interrupted": False, "cancelled": False}
    reply_parts: list[str] = []  # D1 会话上报：assistant 回复流式累计

    async def produce():
        try:
            config = _trace_patch({"configurable": {"thread_id": thread_id, **(ctx or {})}}, thread_id)
            async for mode, chunk in graph.astream(graph_input, config=config, stream_mode=["messages", "updates"]):
                if mode == "messages":
                    msg, _meta = chunk
                    text = getattr(msg, "content", None)
                    if text and isinstance(text, str) and msg.__class__.__name__ == "AIMessageChunk":
                        from app.graph.style import strip_emoji

                        clean = strip_emoji(text)
                        reply_parts.append(clean)
                        await queue.put(_sse("token", {"text": clean}))
                elif mode == "updates":
                    for node, update in chunk.items():
                        if node == "__interrupt__":
                            # interrupt 更新：把审批请求原文发给前端
                            state["interrupted"] = True
                            for itr in update:
                                value = getattr(itr, "value", {})
                                await queue.put(_sse("approval_request", {
                                    "interrupt_id": str(getattr(itr, "id", "")),
                                    "value": value,
                                }))
                            continue
                        msgs = update.get("messages", []) if isinstance(update, dict) else []
                        for m in msgs:
                            cls = m.__class__.__name__
                            if cls == "AIMessage" and getattr(m, "tool_calls", None):
                                for tc in m.tool_calls:
                                    await queue.put(_sse("tool_start", {"name": tc["name"], "args": tc.get("args", {})}))
                            elif cls == "ToolMessage":
                                content = str(m.content)[:200]
                                await queue.put(_sse("tool_end", {"name": m.name, "preview": content}))
        except AllEndpointsFailedError as e:
            if run_id:
                await runs.finish(run_id, "failed", error=str(e)[:300])
            await queue.put(_sse("error", {"message": "模型服务暂不可用，请稍后重试", "detail": str(e)[:200]}))
        except asyncio.CancelledError:
            # 被取消（宽限到期/同 thread 新请求顶替）：标 cancelled——此前穿到
            # finally 被标 succeeded，与宽限取消路径的 finish 竞态（2026-10-11 修正）
            state["cancelled"] = True
            if run_id:
                try:
                    await runs.finish(run_id, "cancelled", error="run_cancelled")
                except Exception:  # noqa: BLE001 —— 取消路径的状态落库尽力而为
                    pass
            raise
        except Exception as e:
            log.exception("graph run failed")
            if run_id:
                await runs.finish(run_id, "failed", error=str(e)[:300])
            await queue.put(_sse("error", {"message": "处理请求时出错", "detail": str(e)[:200]}))
        finally:
            await queue.put(SENTINEL)
            if run_id:
                from app.observability import metrics as _m

                if state["interrupted"]:
                    # interrupted 是终态：等待用户回复"同意/拒绝"再起新 run 恢复，
                    # 挂起不占并发额度（否则两次审批请求就把用户锁死）
                    await runs.finish(run_id, "interrupted")
                    _m.inc("agent_runs_finished_total", {"status": "interrupted"})
                elif not state["cancelled"]:
                    await runs.finish(run_id, "succeeded")
                    _m.inc("agent_runs_finished_total", {"status": "succeeded"})
            if run_id:
                _unregister_run(thread_id, run_id)

    producer = asyncio.create_task(produce())
    if run_id:
        _register_run(thread_id, run_id, producer)
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=cfg_heartbeat)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if item is SENTINEL:
                break
            yield item
        if state["interrupted"]:
            yield _sse("suspended", {"message": "等待人工审批，请调用 /task/resume"})
        else:
            yield _sse("done", {"ledger": _state["pool"].ledger.summary()})
    finally:
        # 客户端断开（GeneratorExit）：不立刻杀 run，进入断连宽限；正常结束则清理
        if run_id and run_id in _producers and not producer.done():
            grace = get_config().limits.sse_disconnect_grace_seconds
            _schedule_grace_cancel(thread_id, run_id, grace)
        else:
            _producers.pop(run_id, None)
            _cancel_grace(thread_id)


def _sse_response(graph, graph_input, thread_id: str, cfg, ctx: dict | None = None,
                  run_id: str | None = None):
    return StreamingResponse(
        _stream(graph, graph_input, thread_id, cfg.limits.sse_heartbeat_seconds, ctx, run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/task/sse")
async def task_sse(request: Request):
    identity = deps.verify_request(request)  # 401 直接抛出
    body = await request.json()
    message = str(body.get("message", "")).strip()
    thread_id = str(body.get("thread_id", ""))[:64]
    if not message or not thread_id:
        return JSONResponse(status_code=422, content={"error": "message 和 thread_id 必填"})
    graph = _get_graph(identity.role)
    if graph is None:
        return JSONResponse(status_code=503, content={"error": "服务启动中"})
    await threads.bind_or_verify(thread_id, identity.user_id, identity.role)
    # 同 thread 新请求：立即取消仍在跑的旧 run（宽限期/仍连线的 producer）——
    # 2026-10-11 实录：切界面重发后两 run 并发写 checkpoint、token 双烧
    superseded = _cancel_thread_run(thread_id)
    if superseded:
        log.info("thread %s: run %s superseded by new request", thread_id, superseded)
    cfg = get_config()
    if await runs.active_count(identity.user_id) >= cfg.limits.max_concurrent_runs_per_user:
        return JSONResponse(status_code=429, content={"error": "运行中的任务过多，请稍后再试"})
    run_id = await runs.create(thread_id, identity.user_id)
    graph_input = {"messages": [{"role": "user", "content": message}]}
    ctx = {
        "user_id": identity.user_id,
        "memory_manager": _state.get("memory_manager"),
        "run_token_budget": cfg.limits.run_token_budget,
        # 写侧工具转发 Go API 用原始凭证（compat._ctx 同款；主端点此前漏注=写侧必失败）
        "auth_header": request.headers.get("authorization", ""),
    }
    return _sse_response(graph, graph_input, thread_id, cfg, ctx, run_id)


@app.post("/task/resume")
async def task_resume(request: Request):
    """审批恢复：approved=True 继续执行 L3 工具，False 走拒绝分支。"""
    identity = deps.verify_request(request)
    body = await request.json()
    thread_id = str(body.get("thread_id", ""))[:64]
    approved = bool(body.get("approved"))
    by = identity.user_id
    if not thread_id:
        return JSONResponse(status_code=422, content={"error": "thread_id 必填"})
    graph = _get_graph(identity.role)
    if graph is None:
        return JSONResponse(status_code=503, content={"error": "服务启动中"})
    await threads.bind_or_verify(thread_id, identity.user_id, identity.role)
    from langgraph.types import Command

    rec = await runs.get(thread_id)
    run_id = rec["run_id"] if rec and rec["status"] == "running" else None
    _cancel_grace(thread_id)
    graph_input = Command(resume={"approved": approved, "by": by})
    ctx = {"user_id": identity.user_id, "memory_manager": _state.get("memory_manager"),
           # L3 批准后的真正执行步同样需要透传凭证
           "auth_header": request.headers.get("authorization", "")}
    return _sse_response(graph, graph_input, thread_id, get_config(), ctx, run_id)


@app.post("/task/recover")
async def task_recover(request: Request):
    """断点恢复（XR-1）：服务重启被隔离为 resumable 的 run，从该 thread 最后
    checkpoint 续跑（astream(None)）。新建接替 run（凭证透传——重放写侧工具
    需要用户 JWT）；旧 run 退役（error=crash_recovered，审计不丢）。恢复失败
    （如 checkpoint 版本漂移）由 produce 的异常路径落 failed——宁可弃恢复
    不可错恢复。"""
    identity = deps.verify_request(request)
    body = await request.json()
    thread_id = str(body.get("thread_id", ""))[:64]
    if not thread_id:
        return JSONResponse(status_code=422, content={"error": "thread_id 必填"})
    graph = _get_graph(identity.role)
    if graph is None:
        return JSONResponse(status_code=503, content={"error": "服务启动中"})
    await threads.bind_or_verify(thread_id, identity.user_id, identity.role)
    rec = await runs.get(thread_id)
    if not rec or rec["status"] != "resumable":
        status = rec["status"] if rec else "无记录"
        return JSONResponse(status_code=409, content={
            "error": f"该会话最近 run 状态为 {status}，不可恢复（仅 resumable）"})
    cfg = get_config()
    if await runs.active_count(identity.user_id) >= cfg.limits.max_concurrent_runs_per_user:
        return JSONResponse(status_code=429, content={"error": "运行中的任务过多，请稍后再试"})
    new_run = await runs.create(thread_id, identity.user_id)
    await runs.retire(rec["run_id"], "crash_recovered")
    ctx = {
        "user_id": identity.user_id,
        "memory_manager": _state.get("memory_manager"),
        "run_token_budget": cfg.limits.run_token_budget,
        "auth_header": request.headers.get("authorization", ""),
    }
    # graph_input=None：LangGraph 从最新 checkpoint 续跑（断点恢复语义）
    return _sse_response(graph, None, thread_id, cfg, ctx, new_run)


@app.get("/task/result")
async def task_result(request: Request, thread_id: str):
    """断连/刷新后按 thread 拉取最近一次 run 的结果与状态。"""
    identity = deps.verify_request(request)
    rec = await runs.get(thread_id)
    if rec is None or (rec["user_id"] != identity.user_id and identity.role != "admin"):
        return JSONResponse(status_code=404, content={"error": "run 不存在"})
    graph = _get_graph(identity.role)
    reply = None
    if graph is not None:
        try:
            from langchain_core.messages import AIMessage

            snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            for m in reversed(snap.values.get("messages", []) if snap.values else []):
                if isinstance(m, AIMessage) and m.content:
                    reply = m.content if isinstance(m.content, str) else str(m.content)
                    break
        except Exception:
            log.exception("aget_state failed")
    return {
        "run_id": rec["run_id"],
        "status": rec["status"],
        "error": rec["error"],
        "reply": reply,
    }


if __name__ == "__main__":
    import uvicorn

    c = get_config()
    uvicorn.run(app, host=c.http_host, port=c.http_port)
