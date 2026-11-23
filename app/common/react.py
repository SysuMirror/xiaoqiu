"""ReAct 循环共用件（契约唯一）：chat 与 ops 两图的 agent⇄tools 环。

两面的 agent 节点语义刻意不同（chat=池化回落+token 预算+去 emoji；ops=流式聚合+
运行时换模型重建图）——强行统一需要 N 个钩子，比重复更糟。收敛的只有稳定同构
部分：tool_calls 路由与图接线；节点本体由调用方注入。
"""
from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

log = logging.getLogger(__name__)


def react_route(state) -> Literal["tools", "__end__"]:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


class IdempotentToolNode:
    """ToolNode 的幂等替身（chat 面 XR-2）：idem 集合内的工具按
    (thread_id, tool_call_id) 台账对账——断点恢复/HITL 恢复重入同一 superstep
    时不重复执行副作用，直接回缓存结果。

    键的取法（取舍可盘问）：tool_call_id 是模型生成的调用标识，checkpoint 里的
    AIMessage 带着它，LangGraph 重放未落账 superstep 时同一 id 重发——天然唯一，
    连续两次合法调用（如 toggle 点赞再取消）是两个不同 id，永不误判；比
    (tool,args) 哈希键（会误杀同参数的合法重复调用）正确。
    只读工具不进 idem 集合：零台账开销直跑。
    """

    def __init__(self, tools, idem_names: set[str]):
        self.tools = {t.name: t for t in tools}
        self.idem = set(idem_names)

    async def __call__(self, state, config):
        from app.memory import ledger

        thread_id = (config.get("configurable") or {}).get("thread_id") or ""
        last = state["messages"][-1]
        outs: list[ToolMessage] = []
        for tc in getattr(last, "tool_calls", []) or []:
            name = str(tc.get("name", ""))
            call_id = str(tc.get("id", "") or "")
            tool = self.tools.get(name)
            if tool is None:
                outs.append(ToolMessage(
                    content=f"未知工具 {name}（不在本会话可用工具集）",
                    tool_call_id=call_id))
                continue
            guarded = name in self.idem and thread_id and call_id
            if guarded:
                try:
                    cached = await ledger.find_tool_exec(thread_id, call_id)
                except Exception:  # noqa: BLE001 —— 对账失败按未执行处理（宁可重执行不可漏执行）
                    cached = None
                if cached is not None:
                    outs.append(ToolMessage(content=cached, tool_call_id=call_id))
                    continue
            try:
                out = await tool.ainvoke(dict(tc.get("args") or {}))
                content = getattr(out, "content", out)
                content = str(content)
            except Exception as e:  # noqa: BLE001 —— 与 ToolNode 同语义：工具失败进 ToolMessage 不炸图
                content = f"工具执行失败: {str(e)[:200]}"
            if guarded and not content.startswith("工具执行失败"):
                try:
                    await ledger.record_tool_exec(thread_id, call_id, name, content)
                except Exception:  # noqa: BLE001 —— ADR-7 best-effort
                    pass
            outs.append(ToolMessage(content=content, tool_call_id=call_id))
        return {"messages": outs}


def wire_react_loop(g: StateGraph, agent_node, tools,
                    tool_node=None) -> None:
    """把 agent⇄tools 环加进图：agent/tools 节点 + 条件路由 + 回边。
    入口与前置节点（如 chat 的 prep）由调用方接。tool_node 可注入替身
    （chat 面传 IdempotentToolNode；缺省标准 ToolNode——ops 面零改动）。"""
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node or ToolNode(tools))
    g.add_conditional_edges("agent", react_route, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
