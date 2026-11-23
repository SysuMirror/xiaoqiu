"""chat 域记忆/追溯工具。依赖经 configurable 注入：store 由图编译注入，pool/user_id 在 config。"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.memory import ledger


def _cfg(config: RunnableConfig) -> dict:
    return config.get("configurable") or {}


@tool
async def memory_save(text: str, config: RunnableConfig) -> str:
    """记住用户偏好或长期有效的信息（写记忆库，自动去重与合并）。

    Args:
        text: 要记住的内容，一句完整的话，如「用户喜欢简洁的回答」
    """
    cfg = _cfg(config)
    manager = cfg.get("memory_manager")
    user_id = cfg.get("user_id", "")
    if manager is None or not user_id:
        return "错误：记忆系统未就绪"
    action = await manager.save(user_id, text, thread_id=cfg.get("thread_id"))
    return {
        "new": "已记住。",
        "merged": "已记住（与已有记忆合并）。",
        "updated": "已更新原有记忆。",
        "duplicate": "这条我已经知道了。",
        "rejected": "内容为空，未保存。",
    }.get(action, "已保存。")


@tool
async def list_recent_actions(config: RunnableConfig) -> str:
    """查询小秋最近的自主操作记录（停止服务、发邮件、定时任务等）。"""
    rows = await ledger.recent(10)
    if not rows:
        return "最近没有执行过自主操作。"
    lines = [
        f"- [{r['created_at'][:16]}] {r['action_type']} -> {r['target'] or '-'} : {r['result']}"
        for r in rows
    ]
    return "最近操作记录：\n" + "\n".join(lines)
