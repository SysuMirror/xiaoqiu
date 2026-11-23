"""submit_feedback：用户反馈直达负责人邮箱（chat 域 L2，对外副作用 = 发一封邮件）。

AI 整理：LLM 先把用户口述整理为 分类/标题/摘要 再调工具，用户原话一并附上供核对，
防止整理失真。限流：每用户 1 条/小时（防刷负责人邮箱）。
邮件通道未配置（log 模式）时如实告知"已记录"，不谎称已发送。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.config import get_config
from app.memory import ledger
from app.tools.notify import mail

log = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}  # user_id -> monotonic ts
COOLDOWN_SECONDS = 3600

CATEGORIES = ("bug", "建议", "咨询", "其他")


@tool
async def submit_feedback(
    category: str,
    title: str,
    summary: str,
    original: str = "",
    config: RunnableConfig = None,
) -> str:
    """提交用户反馈：站点负责人邮箱会收到整理后的邮件。用户报 bug/提建议/表达不满时使用。

    调用前先完成整理：category 归类、title 一句话概括、summary 客观重述问题与诉求，
    original 保留用户原话关键部分。忠实转述，不要夸大，也不要替用户编造诉求。

    Args:
        category: 分类，只能是 bug / 建议 / 咨询 / 其他 之一
        title: 一句话标题，40 字以内
        summary: 整理后的问题描述与诉求，500 字以内
        original: 用户原话的关键部分，300 字以内，可留空
    """
    cfg_ctx = (config.get("configurable") or {}) if config else {}
    user_id = str(cfg_ctx.get("user_id", "") or "匿名用户")

    cat = category if category in CATEGORIES else "其他"
    ttl = (title or "").strip()[:40] or "（无标题）"
    smr = (summary or "").strip()[:500]
    org = (original or "").strip()[:300]

    now = time.monotonic()
    last = _last_sent.get(user_id)
    if last is not None and now - last < COOLDOWN_SECONDS:
        wait = int((COOLDOWN_SECONDS - (now - last)) / 60) + 1
        return (f"反馈已收到，但每人每小时只能提交 1 条（约 {wait} 分钟后可再发）。"
                "此条未转发；如内容重要，请稍后重新提交。")
    _last_sent[user_id] = now

    when = datetime.now(ZoneInfo(get_config().timezone)).strftime("%Y-%m-%d %H:%M")
    html = mail.render(
        "feedback", category=cat, title=ttl, summary=smr,
        original=org or "（未附）", user=user_id, time=when,
    )
    outcome = await mail.send(
        "feedback",
        subject=f"[小秋反馈][{cat}] {ttl}",
        html=html,
    )
    status = {"sent": "success", "deduped": "skipped", "rejected": "rejected"}.get(outcome, outcome)
    await ledger.record(
        user_id, "feedback", status, target="owner",
        args_summary={"category": cat, "title": ttl},
        detail=smr[:200], thread_id=cfg_ctx.get("thread_id"),
    )
    if outcome == "sent" and mail.get_mail_cfg().mode == "smtp":
        return "已整理并发送给站点负责人，感谢反馈。"
    return "反馈已记录，站点负责人会定期查看。感谢反馈。"
