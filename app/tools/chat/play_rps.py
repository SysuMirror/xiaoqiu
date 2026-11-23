"""play_rps：对话里直接来一局剪刀石头布（chat 域 L2）。

赢=没奖励，输=封号 5 分钟 + 登录状态作废。出什么拳由用户说了算，LLM 只负责
从对话提取并如实传参；执法对象来自 JWT（configurable.user_id），与 HTTP 端点
同一条 play_core 管道，不存在代他人受罚的路径。
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.game import GameError, play_core

_CN = {"rock": "石头", "scissors": "剪刀", "paper": "布"}


@tool
async def play_rps(move: str, config: RunnableConfig = None) -> str:
    """和用户玩一局剪刀石头布。规则：赢了没奖励，输了封号 5 分钟（当前会话保留，
    期间无法重新登录）。若用户正处于封禁期，这一局自动成为减刑局：赢刑期 -5 分钟
    （剩余不足 5 分钟则当场释放），平局不变，输了刑期 +5 分钟——无论结果都要把
    剩余刑期讲清楚。

    用户提出猜拳或说出自己的拳（如"我出石头""来玩剪刀石头布"）时调用；
    用户的拳以他原话为准，不要替用户编造或更改。既然用户开口就是要玩，
    直接开局即可，不必追问确认；在调用后的回复里自然带上规则说明与结果宣布。

    Args:
        move: 用户出的拳，只能是 rock(石头) / scissors(剪刀) / paper(布) 之一
    """
    cfg_ctx = (config.get("configurable") or {}) if config else {}
    user_id = str(cfg_ctx.get("user_id", "") or "")
    try:
        d = await play_core(user_id, move, admin_exempt=user_id.startswith("a"))
    except GameError as e:
        return f"（本局未能开始：{e}）请如实转告用户。"
    me, ai = _CN.get(d["user_move"], d["user_move"]), _CN.get(d["ai_move"], d["ai_move"])
    left = d.get("remaining_after", 0)
    left_txt = f"{left // 60} 分 {left % 60} 秒" if left else "0 秒（已解封）"
    if d.get("sentence"):
        mode = d["sentence"]["mode"]
        if mode == "commute":
            base = (f"结果：用户出{me}，你出{ai}，减刑局赢了，刑期 -5 分钟，剩余 {left_txt}。"
                    if left else
                    f"结果：用户出{me}，你出{ai}，减刑局赢了，剩余不足 5 分钟直接清零——当场释放！")
            return base + "请如实宣布结果，讲清现在剩余刑期。"
        if mode == "extend":
            return (f"结果：用户出{me}，你出{ai}，减刑局输了，刑期 +5 分钟，剩余 {left_txt}。"
                    "请如实宣布结果，讲清现在剩余刑期。")
        return (f"结果：用户出{me}，你出{ai}，平局，刑期不变，剩余 {left_txt}。请如实宣布。")
    if d["result"] == "win":
        return (f"结果：用户出{me}，你出{ai}，用户赢了。奖励：没有（规则如此）。"
                "请宣布结果，并明确说明没有任何奖励。")
    if d["result"] == "draw":
        return f"结果：用户出{me}，你出{ai}，平局，无事发生。请简短宣布。"
    if d["banned"]:
        return (f"结果：用户出{me}，你出{ai}，用户输了。已执行封号 5 分钟，"
                "期间无法重新登录（当前会话保留）。"
                "请如实宣布处罚结果：封号 5 分钟，到期自动解封。")
    return (f"结果：用户出{me}，你出{ai}，用户输了。{d['note'] or '本次免于封号'}。请宣布结果。")
