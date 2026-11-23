"""旧小秋（Go/Eino）API 兼容层：前端零改动接入新 agent。

契约（docs of SSE-SMART-QIU，v2 扩展见 docs/XIAOQIU_CLIENT_V2.md）：
- GET  /api/v1/agent/conversation/bootstrap → {code,msg,data:{session_id,title,messages[]}}
  无 session_id 时恢复用户最近会话（豆包行为），无历史则开新会话
- POST /api/v1/agent/conversation/close
- POST /api/v1/agent/task/sse  body {task, session_id}
  事件: content{schema_version,mode:append|final,text,run_id?} / status{tool,args} / ping
        / error{code,message,request_id}
  v2 真流式：最终回答逐 token append 外发；工具调用发 status（前端渲染步骤条）；
  final 为全文校准并携带 run_id（消息赞/踩反馈绑定用）
- POST /api/v1/agent/task     同步版
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import runtime
from app.api import deps
from app.config import get_config
from app.memory import ledger, runs, threads

# 存量缺陷修复（I17 ruff 扫出）：三条收尾路径用 metrics 而未导入——NameError 被 finally 吞，
# agent_runs_finished_total 在 compat 路径从未上图
from app.observability import metrics

log = logging.getLogger(__name__)


def _cancel_grace(thread_id):
    runtime.cancel_grace(thread_id)


def _get_graph(role=None):
    return runtime.get_graph(role)


def _producers():
    return runtime.producers


def _state():
    return runtime.state


def _ok(data=None) -> dict:
    return {"code": 0, "msg": "ok", "data": data or {}}


async def bootstrap(request: Request):
    identity = deps.verify_request(request)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    session_id = str(body.get("session_id") or "")[:64]
    if not session_id:
        # 豆包行为：打开即恢复最近会话；无历史会话则开新会话
        session_id = await threads.latest_thread(identity.user_id) or uuid.uuid4().hex
    graph = _get_graph(identity.role)
    if graph is None:
        return JSONResponse(status_code=503, content={"code": 50301, "msg": "服务启动中"})
    await threads.bind_or_verify(session_id, identity.user_id, identity.role)

    messages = []
    try:
        snap = await graph.aget_state({"configurable": {"thread_id": session_id}})
        from langchain_core.messages import AIMessage, HumanMessage

        for m in (snap.values or {}).get("messages", []):
            if isinstance(m, HumanMessage) and m.content:
                messages.append({"role": "user", "content": str(m.content)})
            elif isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                messages.append({"role": "assistant", "content": str(m.content)})
        messages = messages[-100:]
    except Exception:  # noqa: BLE001 —— 历史读取失败不阻塞 bootstrap
        pass
    title = await threads.get_title(session_id)
    return _ok({"session_id": session_id, "title": title, "segment_seq": 0,
                "archived": False, "messages": messages})


async def close(request: Request):
    deps.verify_request(request)
    body = await request.json()
    thread_id = str(body.get("session_id", ""))[:64]
    if thread_id:
        await threads.close_thread(thread_id)
    return _ok()


def _ctx(identity, request: Request, token_budget: int | None = None) -> dict:
    """图运行上下文：身份/记忆/预算 + 原始凭证（写侧工具转发 Go API 用；
    configurable 不进 LLM 上下文，凭证只被工具代码消费；thread_id 由调用方并入）。"""
    ctx = {"user_id": identity.user_id, "memory_manager": _state().get("memory_manager"),
           "auth_header": request.headers.get("authorization", "")}
    if token_budget:
        ctx["run_token_budget"] = token_budget
    return ctx


async def task_sync(request: Request):
    identity = deps.verify_request(request)
    body = await request.json()
    task = str(body.get("task", "")).strip()
    thread_id = str(body.get("session_id", ""))[:64]
    if not task or not thread_id:
        return JSONResponse(status_code=422, content={"code": 40001, "msg": "task/session_id 必填"})
    graph = _get_graph(identity.role)
    await threads.bind_or_verify(thread_id, identity.user_id, identity.role)
    await threads.touch(thread_id)
    cfg = get_config()
    ctx = _ctx(identity, request, cfg.limits.run_token_budget)
    config = runtime.trace_patch({"configurable": {"thread_id": thread_id, **ctx}}, thread_id)
    result = await graph.ainvoke({"messages": [{"role": "user", "content": task}]}, config=config)
    from langchain_core.messages import AIMessage

    reply = ""
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
            reply = m.content if isinstance(m.content, str) else str(m.content)
            break
    _spawn_title(thread_id, identity.user_id, task)
    return _ok({"answer": reply})


APPROVE_WORDS = {"同意", "批准", "确认", "批准执行", "同意执行", "approve", "yes", "y", "好的"}
REJECT_WORDS = {"拒绝", "不同意", "取消", "驳回", "reject", "no", "n"}


async def _pending_interrupt(graph, thread_id: str):
    """若 thread 有待审批 interrupt，返回其 value（dict），否则 None。"""
    try:
        snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        tasks = getattr(snap, "tasks", ()) or ()
        for t in tasks:
            if getattr(t, "interrupts", None):
                intr = t.interrupts[0]
                return getattr(intr, "value", {}) or {}
        return None
    except Exception:  # noqa: BLE001 —— 状态读取失败按无中断处理
        return None


async def task_sse(request: Request):
    identity = deps.verify_request(request)
    body = await request.json()
    task = str(body.get("task", "")).strip()
    thread_id = str(body.get("session_id", ""))[:64]
    if not task or not thread_id:
        return JSONResponse(status_code=422, content={"code": 40001, "msg": "task/session_id 必填"})
    graph = _get_graph(identity.role)
    if graph is None:
        return JSONResponse(status_code=503, content={"code": 50301, "msg": "服务启动中"})
    await threads.bind_or_verify(thread_id, identity.user_id, identity.role)
    await threads.touch(thread_id)
    cfg = get_config()

    # 审批闭环：thread 挂起中 + 用户回复同意/拒绝 → 恢复执行（前端零改动的审批入口）
    pending = await _pending_interrupt(graph, thread_id)
    if pending and task.lower() in APPROVE_WORDS | REJECT_WORDS:
        approved = task.lower() in APPROVE_WORDS
        from langgraph.types import Command

        metrics.inc("agent_runs_created_total", {"role": identity.role})
        metrics.inc("agent_approvals_total", {"decision": "approved" if approved else "rejected"})
        run_id = await runs.create(thread_id, identity.user_id)
        ctx = _ctx(identity, request)
        return StreamingResponse(
            _compat_stream(graph, Command(resume={"approved": approved, "by": identity.user_id}),
                           thread_id, ctx, run_id, cfg.limits.sse_heartbeat_seconds, task),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 同 thread 新请求：立即取消仍在跑的旧 run（仍连线的 producer——前端切界面
    # 后旧 fetch 不 abort、run 在后台继续；重发即双跑双烧/并发写 checkpoint，10-11 实录）
    superseded = runtime.cancel_thread_run(thread_id)
    if superseded:
        log.info("thread %s: run %s superseded by new request", thread_id, superseded)
    if await runs.active_count(identity.user_id) >= cfg.limits.max_concurrent_runs_per_user:
        return JSONResponse(status_code=429, content={"code": 42901, "msg": "运行中的任务过多"})
    metrics.inc("agent_runs_created_total", {"role": identity.role})
    run_id = await runs.create(thread_id, identity.user_id)
    ctx = _ctx(identity, request, cfg.limits.run_token_budget)
    graph_input = {"messages": [{"role": "user", "content": task}]}
    return StreamingResponse(
        _compat_stream(graph, graph_input, thread_id, ctx, run_id,
                       cfg.limits.sse_heartbeat_seconds, task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _spawn_title(thread_id: str, user_id: str, question: str):
    """首轮问答后异步生成会话标题（LLM，失败回落截断问题）。不阻塞对话主链。"""
    if not question.strip():
        return

    async def _worker():
        try:
            if await threads.get_title(thread_id):
                return
            title = ""
            try:
                cfg = get_config().ops_llm
                key = os.environ.get(cfg.api_key_env, "")
                if key:
                    prompt = ("为下面这轮用户与AI助手的对话生成一个不超过12字的中文标题。"
                              "只输出标题本身：不要引号、不要句号结尾、不要任何解释。\n"
                              f"用户提问：{question[:200]}")
                    async with httpx.AsyncClient(timeout=12, trust_env=False) as hc:
                        resp = await hc.post(
                            f"{cfg.base_url.rstrip('/')}/chat/completions",
                            headers={"Authorization": f"Bearer {key}"},
                            json={"model": cfg.models[-1],
                                  "messages": [{"role": "user", "content": prompt}],
                                  # max_tokens 留余量：glm 现强制思考，思考也消耗 token 预算
                                  "max_tokens": 150})
                    if resp.status_code == 200:
                        title = str(resp.json()["choices"][0]["message"]["content"] or "").strip()
            except Exception:  # noqa: BLE001
                title = ""
            title = (title or question.strip()).replace("\n", " ")[:24].strip(" 。，,、：:；;请")
            if title and not await threads.get_title(thread_id):
                await threads.set_title(thread_id, user_id, title)
        except Exception:  # noqa: BLE001 —— 标题失败无副作用
            pass

    asyncio.create_task(_worker())


async def _compat_stream(graph, graph_input, thread_id: str, ctx: dict, run_id: str,
                         heartbeat: int, task_text: str = ""):
    """真流式事件流：agent 文本 token 逐个 append；工具调用发 status；final 全文校准。

    - append 的文本若后面紧跟工具调用，前端把该段折叠为步骤（final 校准兜底）
    - final 携带 run_id，前端消息赞/踩据此绑定
    - 审批 interrupt 语义不变
    """
    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()
    state = {"normal_end": False}

    async def produce():
        request_id = run_id
        t0 = asyncio.get_event_loop().time()
        run_tokens = 0
        first_token_ms: int | None = None
        try:
            config = runtime.trace_patch({"configurable": {"thread_id": thread_id, **ctx}}, thread_id)
            from langchain_core.messages import AIMessage, AIMessageChunk

            from app.graph.style import strip_emoji

            async for mode, chunk in graph.astream(graph_input, config=config,
                                                   stream_mode=["messages", "updates"]):
                if mode == "messages":
                    msg_chunk, meta = chunk
                    if (meta or {}).get("langgraph_node") != "agent":
                        continue
                    if not isinstance(msg_chunk, (AIMessageChunk, AIMessage)):
                        continue
                    content = msg_chunk.content
                    if isinstance(content, str) and content:
                        if first_token_ms is None:
                            first_token_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
                        await queue.put(_sse("content", {
                            "schema_version": 2, "mode": "append",
                            "text": strip_emoji(content)}))
                elif mode == "updates":
                    for node, update in chunk.items():
                        if node == "__interrupt__":
                            for itr in update:
                                val = getattr(itr, "value", {}) or {}
                                args = val.get("args", {}) if isinstance(val, dict) else {}
                                tool = val.get("tool", "操作") if isinstance(val, dict) else "操作"
                                await queue.put(_sse("content", {
                                    "schema_version": 2, "mode": "final",
                                    "text": (f"该操作需要审批：{tool}，参数：{args}。"
                                             "回复「同意」执行，「拒绝」取消。")}))
                            await runs.finish(run_id, "interrupted")
                            await ledger.record(
                                "agent", "approval_pending", "pending",
                                target=str(args.get("target", "")) if isinstance(args, dict) else "",
                                detail=str(args)[:200] if isinstance(args, dict) else "",
                                thread_id=thread_id)
                            await queue.put(SENTINEL)
                            return
                        if node == "agent" and isinstance(update, dict):
                            run_tokens += int(update.get("token_used", 0) or 0)
                            for m in update.get("messages", []) or []:
                                for tc in (getattr(m, "tool_calls", None) or []):
                                    args = tc.get("args", {}) or {}
                                    brief = ", ".join(f"{k}={str(v)[:40]}"
                                                      for k, v in list(args.items())[:2])
                                    await queue.put(_sse("status", {
                                        "tool": tc.get("name", "工具"), "args": brief}))
            # 正常完成：final 校准（从 checkpoint 取权威全文，含 emoji 过滤）
            snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            answer, served_model = "", ""
            for m in reversed((snap.values or {}).get("messages", [])):
                if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                    answer = strip_emoji(m.content if isinstance(m.content, str) else str(m.content))
                    # 实际服务模型（兜底腿服务时与配置意图不同——2026-10-12 实录:
                    # 主端点 401 静默降级直连,台账却记主端点名,三层观测全瞎）
                    served_model = str((getattr(m, "response_metadata", None) or {})
                                       .get("model_name", "") or "")
                    break
            await queue.put(_sse("content", {"schema_version": 2, "mode": "final",
                                             "text": answer, "run_id": run_id}))
            if task_text:
                _spawn_title(thread_id, ctx.get("user_id", ""), task_text)
                from app.memory.auto_extract import spawn_extraction

                spawn_extraction(ctx.get("user_id", ""), thread_id, task_text, answer)
        except asyncio.CancelledError:
            # 被取消（客户端断开由 generator finally 标；这里是同 thread 新请求顶替/
            # 宽限取消兜底）——不标则旧 run 永远 running，用户 active_count 虚高 429
            if run_id:
                try:
                    await runs.finish(run_id, "cancelled", error="run_cancelled")
                    metrics.inc("agent_runs_finished_total", {"status": "cancelled"})
                except Exception:  # noqa: BLE001 —— 取消路径的状态落库尽力而为
                    pass
            raise
        except Exception as e:
            # 不打栈的通用 handler 曾让「connection is closed」全量失败 3 小时无任何日志线索
            # （2026-10-10 池僵尸事故，ops agent 因此误诊）——异常必须落日志
            log.exception("chat run failed rid=%s thread=%s", request_id, thread_id)
            from app.model.pool import AllEndpointsFailedError

            msg = "模型服务暂不可用，请稍后重试" if isinstance(e, AllEndpointsFailedError) else "处理请求时出错"
            await queue.put(_sse("error", {"code": 50000, "message": msg, "request_id": request_id}))
            if run_id:
                await runs.finish(run_id, "failed", error=str(e)[:300])
                metrics.inc("agent_runs_finished_total", {"status": "failed"})
        finally:
            state["normal_end"] = True
            await queue.put(SENTINEL)
            runtime.unregister_run(thread_id, run_id)

        if run_id:
            await runs.finish(run_id, "succeeded")
            metrics.inc("agent_runs_finished_total", {"status": "succeeded"})
            # 成本归因 + 首 token 时延（v3 SLO 数据源）——model 记实际服务模型
            # （兜底腿直连时 response_metadata 才是真相），缺元数据再退配置意图
            try:
                model = served_model or get_config().llm.endpoints[0].model
                await runs.finish_metrics(run_id, run_tokens, first_token_ms, model)
                if first_token_ms is not None:
                    metrics.inc("agent_first_token_seconds_total", {"model": model},
                                first_token_ms / 1000)
                    metrics.inc("agent_first_token_count", {"model": model})
                metrics.inc("agent_llm_run_tokens_total", {"model": model}, run_tokens)
            except Exception:  # noqa: BLE001 —— 观测失败不影响主链
                pass

    producer = asyncio.create_task(produce())
    runtime.register_run(thread_id, run_id, producer)
    try:
        yield _sse("ping", {"schema_version": 2})
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except TimeoutError:
                yield _sse("ping", {"schema_version": 2})
                continue
            if item is SENTINEL:
                break
            yield item
    finally:
        if state.get("normal_end"):
            # 正常结束：等 produce 落库完成，避免 cancelled 竞态覆盖 succeeded。
            # producer 被顶替取消时 await 会抛 CancelledError——一并接住
            _producers().pop(run_id, None)
            if not producer.done():
                try:
                    await producer
                except (Exception, asyncio.CancelledError):  # noqa: BLE001
                    pass
        else:
            # 客户端断开：立即取消（断连场景结果仍在 checkpoint）
            if not producer.done():
                producer.cancel()
            _producers().pop(run_id, None)
            if run_id:
                await runs.finish(run_id, "cancelled", error="client_disconnect")
                metrics.inc("agent_runs_finished_total", {"status": "cancelled"})


async def resume(request: Request):
    """审批恢复（旧契约无此事件，兼容为同步接口返回结果）。"""
    identity = deps.verify_request(request)
    body = await request.json()
    thread_id = str(body.get("session_id", ""))[:64]
    approved = bool(body.get("approved"))
    graph = _get_graph(identity.role)
    from langgraph.types import Command

    ctx = _ctx(identity, request)
    config = runtime.trace_patch({"configurable": {"thread_id": thread_id, **ctx}}, thread_id)
    result = await graph.ainvoke(Command(resume={"approved": approved, "by": identity.user_id}),
                                 config=config)
    from langchain_core.messages import AIMessage

    reply = ""
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
            reply = m.content if isinstance(m.content, str) else str(m.content)
            break
    await ledger.record(identity.user_id, "approval", "success" if approved else "rejected",
                        target=thread_id)
    return _ok({"answer": reply})
