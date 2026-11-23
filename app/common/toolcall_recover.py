"""工具调用漂移兜底：把模型吐成文本的 <tool_call> 块解析回协议层 tool_calls。

背景（2026-10-11 线上实录，thread web-mv253agb-ubccn0 / 22.4 万 token run）：
glm-5.3-flash 在深工具轮+超长上下文下，第 7 轮 sql_post_search 没走 tool_calls
通道，把 GLM 家族训练内部的调用格式直接写成正文：

    <tool_call>sql_post_search
    <arg_key>keyword</arg_key>
    <arg_value>社区公约</arg_value>
    <arg_key>limit</arg_key>
    <arg_value>10</arg_value>
    </tool_call>

用户看到整坨 XML、工具循环就此终止。本模块在 agent 节点输出侧兜底：
漂移 → 解析回 tool_calls 继续正常循环；残缺/工具名不在白名单 → 剥掉漂移段。
对任何 bind_tools 模型的同类漂移通用，不限于 GLM。
"""
from __future__ import annotations

import re

_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_ARG = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.DOTALL)
# 单条消息最多恢复的调用数（防漂移消息里塞一坨）
_MAX_CALLS = 5


def _coerce(v: str):
    """参数值尽量还原类型：工具 schema 多为 int/bool/str，字符串硬塞会被校验拒。"""
    s = v.strip()
    for fn in (int, float):
        try:
            return fn(s)
        except ValueError:
            pass
    return s


def recover_tool_calls(content: str, allowed: set[str]) -> tuple[list[dict] | None, str]:
    """解析 content 里的 <tool_call> 文本块。

    返回 (calls, rest)：
    - content 里没有任何块 → (None, content)：不是这种漂移形态，调用方原样放行
    - 有块：calls=合法调用列表（工具名在 allowed 内；空列表=全都解析失败/不合法），
      rest=剥掉全部块后的剩余正文（漂移段不进用户视野）
    """
    blocks = _BLOCK.findall(content)
    if not blocks:
        return None, content
    calls: list[dict] = []
    for i, block in enumerate(blocks[:_MAX_CALLS]):
        # 工具名 = 块内第一个 <arg_key>（或换行）之前的整段文本
        head = re.split(r"<arg_key>|\n", block, maxsplit=1)[0].strip()
        name = head.strip("`\"' ")
        if not name or name not in allowed:
            continue
        args: dict = {}
        for k, v in _ARG.findall(block):
            args[k.strip()] = _coerce(v)
        calls.append({"name": name, "args": args, "id": f"recovered-{i}"})
    rest = _BLOCK.sub("", content).strip()
    return calls, rest
