"""i50 验收：工具调用漂移兜底 + 同工具连击早停（2026-10-11 线上实录根治）。

事故形态（thread web-mv253agb-ubccn0 / 22.4 万 token run）：glm-5.3-flash
深工具轮下换关键词死循环搜帖（社区公约→用户协议→使用须知→须知→管理→
发帖须知），第 7 轮把调用写成 <tool_call> 文本直出用户面前，循环终止。

T1 解析器：线上原样格式 → 解析回 tool_calls（含数值类型还原）；
   残缺/非白名单工具名 → 剥掉不留用户视野；无块消息 → 原样放行
T2 漂移修复：真 AIMessage 注回 tool_calls；救不回且无正文 → 要求强制收口
T3 连击早停：同工具连续 5 次（参数不同）触发；穿插其它工具不触发
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.common.toolcall_recover import recover_tool_calls
from app.graph.build import MAX_SAME_TOOL_STREAK, apply_drift_recovery, same_tool_streak

ALLOWED = {"sql_post_search", "kb_search"}

# 线上捞到的原样漂移输出（2026-10-11 实录）
DRIFT_RAW = """

<tool_call>sql_post_search
<arg_key>keyword</arg_key>
<arg_value>社区公约</arg_value>
<arg_key>limit</arg_key>
<arg_value>10</arg_value>
</tool_call>"""


def t1_parser():
    calls, rest = recover_tool_calls(DRIFT_RAW, ALLOWED)
    assert calls and len(calls) == 1, calls
    c = calls[0]
    assert c["name"] == "sql_post_search"
    assert c["args"] == {"keyword": "社区公约", "limit": 10}, c["args"]  # 10 已还原 int
    assert rest == "", repr(rest)
    assert c["id"].startswith("recovered-")

    # 非白名单工具名 → 剥掉不留视野
    calls, rest = recover_tool_calls(
        "<tool_call>rm_rf_everything\n<arg_key>x</arg_key>\n<arg_value>1</arg_value>\n</tool_call>",
        ALLOWED)
    assert calls == [] and rest == "", (calls, rest)

    # 多块：一合法一不合法
    calls, rest = recover_tool_calls(
        "前文说明\n<tool_call>kb_search\n<arg_key>q</arg_key>\n<arg_value>怎么发帖</arg_value>\n</tool_call>"
        "\n<tool_call>not_a_tool\n</tool_call>\n后文", ALLOWED)
    assert [c["name"] for c in calls] == ["kb_search"]
    assert calls[0]["args"] == {"q": "怎么发帖"}
    assert [ln for ln in rest.splitlines() if ln.strip()] == ["前文说明", "后文"], repr(rest)

    # 无块 → None（不是这种漂移，调用方原样放行）
    calls, rest = recover_tool_calls("正常回复，没有调用", ALLOWED)
    assert calls is None and rest == "正常回复，没有调用"

    # 多行 arg_value（宽松 DOTALL）
    calls, _ = recover_tool_calls(
        "<tool_call>kb_search\n<arg_key>q</arg_key>\n<arg_value>第一行\n第二行</arg_value>\n</tool_call>",
        ALLOWED)
    assert calls[0]["args"]["q"] == "第一行\n第二行"
    print("[T1] 解析器 OK（原样格式/非白名单/多块/无块/多行值）")


def t2_recovery():
    # 可救：注回 tool_calls，正文剥离
    m = AIMessage(content=DRIFT_RAW)
    assert apply_drift_recovery(m, ALLOWED) is False
    assert m.tool_calls and m.tool_calls[0]["name"] == "sql_post_search"
    assert m.content == ""

    # 救不回且无正文 → 要求强制收口
    m = AIMessage(content="\n<tool_call>幻觉工具\n</tool_call>\n")
    assert apply_drift_recovery(m, ALLOWED) is True
    assert m.content == "" and not getattr(m, "tool_calls", None)

    # 救不回但有正文 → 剥块留正文，不收口
    m = AIMessage(content="基于已有结果：<tool_call>幻觉工具\n</tool_call>回答如下…")
    assert apply_drift_recovery(m, ALLOWED) is False
    assert m.content == "基于已有结果：回答如下…" and not getattr(m, "tool_calls", None)

    # 正常消息不动
    m = AIMessage(content="正常回复")
    assert apply_drift_recovery(m, ALLOWED) is False
    m2 = AIMessage(content="", tool_calls=[{"name": "kb_search", "args": {}, "id": "x"}])
    assert apply_drift_recovery(m2, ALLOWED) is False
    print("[T2] 漂移修复 OK（注回/收口/剥块留文/正常不动）")


def _ai(name, arg="v"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"k": arg}, "id": "t"}])


def t3_streak():
    turn = [
        HumanMessage(content="社区公约在哪"),
        _ai("sql_post_search", "社区公约"), AIMessage(content=""),
        ToolMessage(content="未找到", tool_call_id="t"),
        _ai("sql_post_search", "用户协议"), AIMessage(content=""),
        ToolMessage(content="未找到", tool_call_id="t"),
        _ai("sql_post_search", "使用须知"), AIMessage(content=""),
        ToolMessage(content="未找到", tool_call_id="t"),
        _ai("sql_post_search", "须知"), AIMessage(content=""),
        ToolMessage(content="未找到", tool_call_id="t"),
        _ai("sql_post_search", "管理"), AIMessage(content=""),
    ]
    assert same_tool_streak(turn) == 5
    assert same_tool_streak(turn) >= MAX_SAME_TOOL_STREAK  # 护栏触发

    # 穿插其它工具 → 连击断开
    turn2 = [_ai("sql_post_search"), _ai("kb_search"), _ai("sql_post_search"),
             _ai("sql_post_search"), _ai("sql_post_search")]
    assert same_tool_streak(turn2) == 3 < MAX_SAME_TOOL_STREAK

    # 空轮
    assert same_tool_streak([HumanMessage("hi")]) == 0
    print(f"[T3] 连击早停 OK（同工具连续 5 次触发 / 穿插断开 / 空轮 0）")


async def main():
    t1_parser()
    t2_recovery()
    t3_streak()
    print("i50 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
