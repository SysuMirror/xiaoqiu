"""prep 节点提示词刷新回归：老会话冻结的旧 System 消息必须被当前提示词换掉。

背景（2026-10-10 线上）：「导出某帖之后所有帖子」答错——会话创建早于 sql_read
上线，旧提示词只教了 搜索/最新/统计，模型永远学不到导出翻页纪律，最后拿历史
上下文拼凑清单。根因：_prep_node 原来只在会话首条注入提示词，之后每轮复用
checkpoint 里冻结的旧提示词。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage

from app.graph.build import _prep_node


async def test_first_turn_injects_current_prompt():
    state = {"messages": [HumanMessage(content="hi", id="h1")]}
    update = await _prep_node(state, {})
    new_sys = [m for m in update["messages"] if isinstance(m, SystemMessage)]
    assert len(new_sys) == 1
    assert "sql_read" in new_sys[0].content  # 当前提示词（含导出翻页纪律）
    assert not any(isinstance(m, RemoveMessage) for m in update["messages"])


async def test_stale_system_messages_replaced():
    # 模拟 1.0.36 之前创建的老会话：state 里躺着冻结的旧提示词 + 上一轮的台账注入
    frozen_prompt = SystemMessage(content="旧职责：帖子搜索/最新帖子/统计", id="s1")
    old_ledger = SystemMessage(content="上一轮的台账注入", id="s2")
    state = {"messages": [frozen_prompt, old_ledger,
                          HumanMessage(content="导出帖子", id="h1")]}
    update = await _prep_node(state, {})
    removals = [m for m in update["messages"] if isinstance(m, RemoveMessage)]
    assert sorted(r.id for r in removals) == ["s1", "s2"]
    assert "h1" not in [r.id for r in removals]  # 非系统消息一律不动
    new_sys = [m for m in update["messages"] if isinstance(m, SystemMessage)]
    assert len(new_sys) == 1
    assert "sql_read" in new_sys[0].content
