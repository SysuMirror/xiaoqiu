"""XR（run 断点恢复 + 工具幂等）单测：隔离/退役 SQL 契约、台账幂等助手契约、
IdempotentToolNode 行为（缓存命中/未命中/不同 id/只读直跑/未知工具/异常路径）。"""
import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage

import app.memory.runs as runs_mod
from app.common.react import IdempotentToolNode
from app.memory import ledger

# ---------------- runs：隔离/退役 SQL 契约 ----------------

def test_quarantine_marks_resumable_not_failed():
    """XR-1a：启动对账改判 resumable（弃置 → 可恢复），不再写 failed。"""
    src = inspect.getsource(runs_mod.quarantine_orphans)
    assert "status='resumable'" in src
    assert "finished_at=now()" not in src  # 未终结：恢复前不算完


def test_retire_only_resumable():
    """退役守卫：仅 resumable 可被接替退役（防误伤其他状态）。"""
    src = inspect.getsource(runs_mod.retire)
    assert "AND status='resumable'" in src


# ---------------- ledger：幂等对账助手契约 ----------------

def test_tool_exec_ledger_contract():
    """record/find 按键 (thread_id, call_id) 落账与对账（SQL 契约）。"""
    rec = inspect.getsource(ledger.record_tool_exec)
    assert "tool_exec" in rec and "call_id" in rec
    find = inspect.getsource(ledger.find_tool_exec)
    assert "args_summary->>'call_id'=$2" in find
    assert "result='success'" in find


# ---------------- IdempotentToolNode ----------------

class _FakeTool:
    def __init__(self, name, result="OK", raise_exc=None):
        self.name = name
        self._result = result
        self._raise = raise_exc
        self.calls = 0

    async def ainvoke(self, args):
        self.calls += 1
        if self._raise:
            raise self._raise
        return self._result


def _state_with_call(name, call_id, args=None):
    return {"messages": [AIMessage(content="", tool_calls=[
        {"name": name, "args": args or {"x": 1}, "id": call_id,
         "type": "tool_call"}])]}


def _stub_ledger(monkeypatch, cached=None):
    recorded = []

    async def fake_find(thread_id, call_id):
        return cached if cached.get(call_id) else None

    async def fake_record(thread_id, call_id, tool, content):
        recorded.append((thread_id, call_id, tool, content))

    monkeypatch.setattr(ledger, "find_tool_exec", fake_find)
    monkeypatch.setattr(ledger, "record_tool_exec", fake_record)
    return recorded


def test_idem_first_execution_records(monkeypatch):
    tool = _FakeTool("create_post", result="帖子已发布 id=99")
    recorded = _stub_ledger(monkeypatch, cached={})
    node = IdempotentToolNode([tool], {"create_post"})
    out = asyncio.run(node(_state_with_call("create_post", "call_1"),
                           {"configurable": {"thread_id": "t1"}}))
    assert tool.calls == 1
    assert out["messages"][0].content == "帖子已发布 id=99"
    assert recorded == [("t1", "call_1", "create_post", "帖子已发布 id=99")]


def test_idem_replay_returns_cached_no_reexecution(monkeypatch):
    """重放同一 tool_call_id：不执行工具，直接回缓存结果（副作用零重复）。"""
    tool = _FakeTool("create_post", result="new")
    _stub_ledger(monkeypatch, cached={"call_1": "帖子已发布 id=99（缓存）"})
    node = IdempotentToolNode([tool], {"create_post"})
    out = asyncio.run(node(_state_with_call("create_post", "call_1"),
                           {"configurable": {"thread_id": "t1"}}))
    assert tool.calls == 0  # 未重新执行
    assert "缓存" in out["messages"][0].content


def test_idem_different_call_id_executes(monkeypatch):
    """不同 id（合法的连续两次调用）互不误判——键的正确性核心。"""
    tool = _FakeTool("toggle_like_post", result="toggled")
    recorded = _stub_ledger(monkeypatch, cached={"call_1": "done"})
    node = IdempotentToolNode([tool], {"toggle_like_post"})
    asyncio.run(node(_state_with_call("toggle_like_post", "call_2"),
                     {"configurable": {"thread_id": "t1"}}))
    assert tool.calls == 1
    assert recorded[0][1] == "call_2"


def test_readonly_tool_skips_ledger(monkeypatch):
    """只读工具（不在 idem 集）零台账开销直跑。"""
    tool = _FakeTool("sql_read", result="rows")
    recorded = _stub_ledger(monkeypatch, cached={})
    node = IdempotentToolNode([tool], {"create_post"})
    asyncio.run(node(_state_with_call("sql_read", "call_9"),
                     {"configurable": {"thread_id": "t1"}}))
    assert tool.calls == 1 and not recorded


def test_unknown_tool_and_tool_error(monkeypatch):
    ok = _FakeTool("create_post")
    boom = _FakeTool("submit_feedback", raise_exc=RuntimeError("smtp down"))
    _stub_ledger(monkeypatch, cached={})
    node = IdempotentToolNode([ok, boom], {"create_post", "submit_feedback"})
    out = asyncio.run(node(
        {"messages": [AIMessage(content="", tool_calls=[
            {"name": "nmap", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "submit_feedback", "args": {}, "id": "c2", "type": "tool_call"},
        ])]},
        {"configurable": {"thread_id": "t1"}}))
    assert "未知工具" in out["messages"][0].content
    assert "工具执行失败" in out["messages"][1].content  # 异常进 ToolMessage 不炸图


def test_ledger_find_failure_falls_through(monkeypatch):
    """对账失败按未执行处理（宁可重执行不可漏执行）。"""
    tool = _FakeTool("create_post", result="posted")

    async def broken_find(thread_id, call_id):
        raise RuntimeError("db down")

    async def fake_record(*a):
        pass

    monkeypatch.setattr(ledger, "find_tool_exec", broken_find)
    monkeypatch.setattr(ledger, "record_tool_exec", fake_record)
    node = IdempotentToolNode([tool], {"create_post"})
    out = asyncio.run(node(_state_with_call("create_post", "call_1"),
                           {"configurable": {"thread_id": "t1"}}))
    assert tool.calls == 1 and out["messages"][0].content == "posted"
