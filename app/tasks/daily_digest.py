"""定时任务：APScheduler 进程内调度，job = 数据收集 + 渲染 + 邮件 + 台账，幂等可重试。"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_config
from app.memory import ledger
from app.tools.forum.sql_templates import _query
from app.tools.notify import mail

log = logging.getLogger(__name__)


def _today_str(tz: ZoneInfo) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


async def digest_already_sent_today() -> bool:
    rows = await ledger.recent(30)
    today = _today_str(ZoneInfo(get_config().timezone))
    return any(
        r["action_type"] == "daily_digest" and r["result"] == "success"
        and r["created_at"][:10] == today
        for r in rows
    )


async def collect_digest_data() -> dict:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    _, stats = await _query(
        "/*T*/ SELECT COUNT(*) AS new_posts, IFNULL(SUM(p.browse_num),0) AS total_browse"
        " FROM posts p WHERE p.post_time >= %s",
        (yesterday,),
    )
    _, hot = await _query(
        "/*T*/ SELECT p.postID, p.title, p.browse_num, p.like_num FROM posts p"
        " WHERE p.post_time >= %s ORDER BY p.browse_num DESC LIMIT 5",
        (yesterday,),
    )
    cols = ["postID", "title", "browse_num", "like_num"]
    hot_list = [dict(zip(cols, row)) for row in hot]
    return {
        "date": yesterday,
        "new_posts": stats[0][0] if stats else 0,
        "total_browse": stats[0][1] if stats else 0,
        "hot": hot_list,
        "generated_at": datetime.now(ZoneInfo(get_config().timezone)).strftime("%Y-%m-%d %H:%M"),
    }


async def run_daily_digest(force: bool = False) -> dict:
    """执行日报任务。返回 {result, detail}；幂等：当日已成功则跳过（force 可覆盖）。"""
    if not force and await digest_already_sent_today():
        await ledger.record("system", "daily_digest", "skipped", detail="already sent today")
        return {"result": "skipped", "detail": "already sent today"}

    last_err: Exception | None = None
    for attempt in range(1, 4):  # 失败重试 3 次
        try:
            data = await collect_digest_data()
            html = mail.render("daily_digest", **data)
            outcome = await mail.send(
                "admins",
                subject=f"[小秋] SSE Market 日报 {data['date']}",
                html=html,
                dedup_key=f"daily_digest:{data['date']}:{attempt}",
            )
            await ledger.record("system", "daily_digest", "success",
                                target=data["date"], detail=f"mail={outcome}")
            return {"result": "success", "detail": f"mail={outcome}"}
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("daily_digest attempt %d failed: %s", attempt, e)
            await asyncio.sleep(1)

    await ledger.record("system", "daily_digest", "failed", detail=str(last_err)[:300])
    return {"result": "failed", "detail": str(last_err)[:200]}
