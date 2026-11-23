"""图状态定义。"""
from __future__ import annotations

import operator
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    token_used: NotRequired[Annotated[int, operator.add]]  # 单 run 累计 token
