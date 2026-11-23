"""chat 图工具轮预算（硬护栏版）单测：本轮计数 / 到顶摘工具 / 同参复读早停。

背景（2026-10-10 连环事故，两版护栏教训）：
- 软护栏 v1：只往 system 追加「立即停止调用工具」劝告——glm-4.5-air 无视，sql_read 75 连发烧 12.5M tokens
- 软护栏 v2 缺陷：tool_rounds 数全历史轮——老会话（导出 100+ 轮）每个新轮都「已耗尽」常态触发
- 硬护栏 v3（现行）：本轮计数 + pool.ainvoke_no_tools 真摘工具 + 同参复读 x3 早停
"""
import asyncio
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph.build import MAX_TOOL_ROUNDS, _agent_node


class _FakePool:
    def __init__(self):
        self.via = None
        self.seen = None

    async def ainvoke_with_fallback(self, llm, tools, messages, config=None):
        self.via = "with_tools"
        self.seen = list(messages)
        return AIMessage(content="ok")

    async def ainvoke_no_tools(self, messages, config=None):
        self.via = "no_tools"
        self.seen = list(messages)
        return AIMessage(content="收口")


def _state(turns: int, system: bool = True, prior_turns: int = 0, same_args: bool = False) -> dict:
    """prior_turns=上一轮（含新 HumanMessage 分界）的工具轮数；turns=本轮。"""
    msgs: list = ([SystemMessage(content="sys")] if system else []) + [HumanMessage(content="q0")]
    for i in range(prior_turns):
        msgs.append(AIMessage(content="", tool_calls=[{"name": "sql_read", "args": {"q": i}, "id": f"p{i}"}]))
        msgs.append(ToolMessage(content="rows", tool_call_id=f"p{i}"))
    msgs.append(AIMessage(content="prior done"))
    msgs.append(HumanMessage(content="q1"))
    for i in range(turns):
        args = {"page": 3} if same_args else {"q": i}
        msgs.append(AIMessage(content="", tool_calls=[{"name": "sql_read", "args": args, "id": f"c{i}"}]))
        msgs.append(ToolMessage(content="rows", tool_call_id=f"c{i}"))
    return {"messages": msgs}


def test_no_force_below_budget_uses_tools():
    p = _FakePool()
    node = _agent_node(p, object(), [])
    asyncio.run(node(_state(MAX_TOOL_ROUNDS - 1), {}))
    assert p.via == "with_tools"
    assert "预算上限" not in str(p.seen[0].content)


def test_force_at_budget_unbinds_tools():
    p = _FakePool()
    node = _agent_node(p, object(), [])
    out = asyncio.run(node(_state(MAX_TOOL_ROUNDS), {}))
    assert p.via == "no_tools"  # 硬停：不再绑工具
    assert "预算上限" in p.seen[0].content and str(MAX_TOOL_ROUNDS) in p.seen[0].content
    assert out["messages"][0].content == "收口"


def test_prior_turn_rounds_do_not_count():
    """老会话历史轮不清零（软护栏缺陷二）：上一轮 30 轮工具，本轮 0 轮 → 不触发。"""
    p = _FakePool()
    node = _agent_node(p, object(), [])
    asyncio.run(node(_state(0, prior_turns=30), {}))
    assert p.via == "with_tools"


def test_repeat_same_args_early_stop():
    """同参复读 x3 早停：即使远未到轮数上限。"""
    p = _FakePool()
    node = _agent_node(p, object(), [])
    asyncio.run(node(_state(3, same_args=True), {}))
    assert p.via == "no_tools"
    assert "复读" in p.seen[0].content


def test_varying_args_below_budget_not_stopped():
    """翻页型循环（参数渐变）在轮数未到顶前不误杀——由轮数上限兜底。"""
    p = _FakePool()
    node = _agent_node(p, object(), [])
    asyncio.run(node(_state(5, same_args=False), {}))
    assert p.via == "with_tools"
