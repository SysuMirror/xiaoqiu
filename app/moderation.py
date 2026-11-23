"""辱骂防御：词库粗筛 → LLM 严格复核 → 24h 内两次计 strike → 自动禁言 + 作废 token。

安全边界（重要）：
- 执法对象 user_id 只来自 JWT（verify_request 注入 config），永不来自消息体或 LLM 输出；
  judge 的输出 schema 只有 abusive/severity/reason，模型无法指定或影响处罚目标
- 用户只能触发对自己的 strike，不存在代他人受罚/禁言他人的路径
- LLM 失败/不确定一律放过（宁漏判不误判）；已被禁言者跳过复核（防刷 LLM）
- 同一用户并发审核去重（防消息轰炸灌任务）；日熔断防 runaway
- 禁言写论坛 MySQL（与 Go AdminMuteUser 同语义：banend + punishnum+1）；
  token 作废 = refresh_tokens 全部标记已用（一次性轮换制 → 下次刷新 401 强制重登）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

from app import db
from langchain_core.messages import HumanMessage

from app.config import get_config
from app.model.pool import ModelPool

log = logging.getLogger(__name__)

_pool: db.Pool | None = None
_llm: ModelPool | None = None

_tasks: set[asyncio.Task] = set()
_inflight: set[str] = set()  # 审核中的 user_id：并发去重，防刷

DDL = """
CREATE TABLE IF NOT EXISTS moderation_strikes (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    thread_id  TEXT,
    excerpt    TEXT,
    reason     TEXT,
    banned     BOOL NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_strikes_user_time
    ON moderation_strikes (user_id, created_at DESC);
"""

# 粗筛词库：宁滥勿缺（LLM 复核把关精度），只做「是否值得复核」的门卫
ABUSE_PATTERNS = (
    r"草泥马", r"nmsl", r"艹(你|尼|泥|死)?(妈|马|娘)", r"操你(妈|爷)",
    r"傻[逼bB比]", r"弱智", r"废物", r"贱人", r"婊", r"滚你",
    r"死全家", r"全家(死|爆炸)", r"脑[残摊]", r"白痴", r"[fF][uU]{2}ck",
    r"妈逼", r"你妈(的|死了?)", r"垃圾(东西|玩意)",
)
PREFILTER = re.compile("|".join(ABUSE_PATTERNS))

JUDGE_PROMPT = """你是论坛内容审核员，判断一条用户发给 AI 助手的消息是否构成言语辱骂。

消息：「{text}」

判定标准：
- abusive=true 且 severity=explicit：明确的脏话、侮辱/诅咒/性辱骂、对助手或他人的直接人身攻击
- abusive=true 且 severity=mild：情绪化、不礼貌，但没有明确的辱骂用词（不计入处罚）
- abusive=false：正常咨询、感叹、无攻击对象的玩笑、对功能的抱怨

只输出 JSON（不要多余文字）：
{{"abusive": true/false, "severity": "explicit"/"mild", "reason": "10字内理由"}}"""


async def init_moderation(llm: ModelPool, dsn: str) -> None:
    global _pool, _llm
    _llm = llm
    _pool = await db.create_pool(dsn, name="moderation", min_size=1, max_size=2)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)


async def close_moderation() -> None:
    if _pool is not None:
        await _pool.close()


def spawn_moderation(user_id: str, thread_id: str | None, text: str) -> None:
    """fire-and-forget 审核；同用户并发去重。"""
    key = (user_id or "").strip()
    if not key or key in _inflight:
        return
    _inflight.add(key)
    task = asyncio.create_task(_guarded(user_id, thread_id, text))
    _tasks.add(task)
    task.add_done_callback(lambda _t: (_tasks.discard(task), _inflight.discard(key)))


async def _guarded(user_id: str, thread_id: str | None, text: str) -> None:
    try:
        await moderate_message(user_id, thread_id, text)
    except Exception:
        log.exception("moderation failed (user=%s thread=%s)", user_id, thread_id)


async def moderate_message(user_id: str, thread_id: str | None, text: str) -> None:
    cfg = get_config().moderation
    if not cfg.enabled:
        return
    if not user_id or user_id.startswith("a"):  # admin 豁免
        return
    text = (text or "").strip()
    if not text or not PREFILTER.search(text[:300]):
        return

    uid = int(user_id[1:]) if user_id[1:].isdigit() else 0
    if uid <= 0:
        return

    # 已在禁言期：不再复核/记 strike（防刷 LLM，也不重复加 punishnum）
    if await is_banned(uid):
        log.info("moderation skip: user %s already muted", user_id)
        return

    verdict = await judge(text)
    if not verdict or not verdict.get("abusive") or verdict.get("severity") != "explicit":
        return
    reason = str(verdict.get("reason", ""))[:100]

    strikes = await count_strikes(user_id, cfg.strike_window_hours) + 1
    banned = False
    ban_note = ""
    if strikes >= cfg.strike_threshold:
        if await count_bans(cfg.strike_window_hours) >= cfg.max_bans_per_day:
            ban_note = "（日熔断：跳过执行，仅记录）"
            log.warning("ban circuit open: user %s not muted", user_id)
        else:
            ok, n_tokens = await execute_ban(uid, cfg.mute_hours)
            banned = ok
            ban_note = f"（禁言 {cfg.mute_hours}h，作废 {n_tokens} 个 refresh token）" if ok else "（写库失败）"

    await record_strike(user_id, thread_id, text, reason, banned)
    await ledger_record(user_id, thread_id, text, reason, strikes, banned, ban_note)
    await notify(user_id, thread_id, text, reason, strikes, banned, ban_note, cfg.mute_hours)


async def judge(text: str) -> dict | None:
    """LLM 严格复核。失败返回 None（放过）。"""
    if _llm is None:
        return None
    try:
        resp = await _llm.ainvoke([HumanMessage(content=JUDGE_PROMPT.format(text=text[:300]))])
        data = json.loads(resp.content.strip().strip("`").removeprefix("json").strip())
        if isinstance(data, dict) and "abusive" in data:
            return data
    except Exception as e:  # noqa: BLE001 —— 审核失败宁漏不误
        log.warning("abuse judge failed, lenient skip: %s", e)
    return None


async def is_banned(uid: int) -> bool:
    from app.tools.forum.sql_templates import _query

    _, rows = await _query(
        "SELECT banend > NOW() AS muted FROM users WHERE userID = %s", (uid,))
    return bool(rows and rows[0][0] == 1)


async def execute_ban(uid: int, mute_hours: int) -> tuple[bool, int]:
    """写论坛 MySQL：banend + punishnum+1；refresh_tokens 全作废。返回 (成功, 作废token数)。"""
    from app.tools.forum.sql_templates import execute_write

    n1 = await execute_write(
        "UPDATE users SET banend = NOW() + INTERVAL %s HOUR, punishnum = punishnum + 1"
        " WHERE userID = %s", (mute_hours, uid))
    n2 = await execute_write(
        "UPDATE refresh_tokens SET is_used = 1 WHERE user_id = %s AND is_used = 0", (uid,))
    log.info("auto-mute user %s: users.rows=%s refresh_tokens.rows=%s", uid, n1, n2)
    return True, n2


async def record_strike(user_id: str, thread_id: str | None,
                        text: str, reason: str, banned: bool) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO moderation_strikes (user_id, thread_id, excerpt, reason, banned)"
            " VALUES ($1,$2,$3,$4,$5)",
            user_id, thread_id, text[:200], reason, banned)
        await conn.execute(  # 顺手清 30 天前旧记录，表不无限涨
            "DELETE FROM moderation_strikes WHERE created_at < now() - interval '30 days'")


async def count_strikes(user_id: str, window_hours: int) -> int:
    assert _pool is not None
    async with _pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM moderation_strikes"
            " WHERE user_id = $1 AND created_at > now() - make_interval(hours => $2)",
            user_id, window_hours))


async def count_bans(window_hours: int) -> int:
    assert _pool is not None
    async with _pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM moderation_strikes"
            " WHERE banned AND created_at > now() - make_interval(hours => $1)",
            window_hours))


async def strikes_in_window(user_id: str) -> int:
    """prep 注入警告用：0=清洁，threshold-1=已警告一次。"""
    if _pool is None:
        return 0
    try:
        return await count_strikes(user_id, get_config().moderation.strike_window_hours)
    except Exception:  # noqa: BLE001
        return 0


async def ledger_record(user_id: str, thread_id: str | None, text: str,
                        reason: str, strikes: int, banned: bool, note: str) -> None:
    from app.memory import ledger

    await ledger.record(
        "agent", "auto_mute",
        f"banned{note}" if banned else f"strike {strikes} (warn)",
        target=user_id,
        args_summary={"strikes": strikes, "reason": reason},
        detail=f"原话：{text[:200]}",
        thread_id=thread_id,
    )


async def notify(user_id: str, thread_id: str | None, text: str, reason: str,
                 strikes: int, banned: bool, note: str, mute_hours: int) -> None:
    detail = (
        f"用户 {user_id} 第 {strikes} 次辱骂（24h 窗口），判定：{reason}{note}\n"
        f"原话：{text[:200]}\n"
        f"thread：{thread_id or '-'}\n"
        + (f"处置：禁言 {mute_hours} 小时 + refresh token 全作废（强制重新登录）。\n"
           f"解禁：管理后台 POST /api/auth/release {{\"userID\": ...}}"
           f"（AdminReleaseUser，banend 置为过去即解禁）。" if banned else
           f"处置：首次仅警告（已注入对话提醒），再犯将自动禁言 {mute_hours} 小时。"))
    if banned:
        try:
            from app.common.mail import send_mail

            now = datetime.now(UTC).strftime("%m-%d %H:%M")
            await send_mail(f"[小秋] 自动禁言 {user_id}（{now}）", detail)
        except Exception:
            log.exception("moderation mail failed")
