"""剪刀石头布：赢了没奖励，输了封号 5 分钟；封禁期开放减刑局（赢 -5min / 输 +5min）。

安全边界（与 moderation 同源）：
- 执法对象只来自 JWT（verify_request），出拳只接受固定枚举；小秋的拳用 secrets
  独立随机生成，与请求内容无关，无法被诱导
- 复用辱骂禁言管道（AdminMuteUser 同语义）：users.banend（登录态保留，
  不作废 refresh token——封禁在重新登录时由 banend 拦截）
- 与辱骂禁言的差异：分钟粒度；首次封禁用 CASE 不缩短既有更长禁言；不动 punishnum
  （输游戏不是违规，不污染论坛处罚记录）；admin 豁免仅免首次封禁，减刑局照常结算
- 战绩落 PG game_rps 表：个人战绩/全局统计的来源，同时是每次封禁/加刑/减刑的审计台账
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from app import db
from fastapi import HTTPException, Request

from app.api import deps

log = logging.getLogger(__name__)

BAN_MINUTES = 5
MOVES = ("rock", "paper", "scissors")
_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
_MOVE_CN = {"rock": "石头", "scissors": "剪刀", "paper": "布"}

_pool: db.Pool | None = None

DDL = """
CREATE TABLE IF NOT EXISTS game_rps (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    user_move  TEXT NOT NULL,
    ai_move    TEXT NOT NULL,
    result     TEXT NOT NULL,
    banned     BOOL NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_game_rps_user_time
    ON game_rps (user_id, created_at DESC);
"""


async def init_game(dsn: str) -> None:
    global _pool
    _pool = await db.create_pool(dsn, name="game", min_size=1, max_size=2)
    async with _pool.acquire() as conn:
        await conn.execute(DDL)


async def close_game() -> None:
    if _pool is not None:
        await _pool.close()


def _ok(data=None) -> dict:
    return {"code": 0, "msg": "ok", "data": data or {}}


def _uid(user_id: str) -> int:
    return int(user_id[1:]) if user_id[1:].isdigit() else 0


async def _ban_remaining_seconds(uid: int) -> int:
    from app.tools.forum.sql_templates import _query

    _, rows = await _query(
        "SELECT GREATEST(TIMESTAMPDIFF(SECOND, NOW(), banend), 0) FROM users WHERE userID = %s",
        (uid,))
    val = rows[0][0] if rows else None
    return int(val) if val is not None else 0


async def _execute_game_ban(uid: int) -> bool:
    """banend = max(既有值, NOW()+5min)（不缩短辱骂长禁）。

    与 moderation.execute_ban 的差异：不动 punishnum（输游戏不是违规）；
    不作废 refresh token（2026-09-24 用户裁决：当前登录态保留，封禁只在
    重新登录时由 banend 拦截；猜拳端点自身另有 423 拒开局）。"""
    from app.tools.forum.sql_templates import execute_write

    n1 = await execute_write(
        "UPDATE users SET banend = CASE"
        " WHEN banend IS NULL OR banend < NOW() THEN NOW() + INTERVAL %s MINUTE"
        " ELSE banend END WHERE userID = %s", (BAN_MINUTES, uid))
    log.info("game-ban user %s: users.rows=%s (login state kept)", uid, n1)
    return n1 > 0


async def _record(user_id: str, user_move: str, ai_move: str, result: str, banned: bool) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO game_rps (user_id, user_move, ai_move, result, banned)"
            " VALUES ($1,$2,$3,$4,$5)",
            user_id, user_move, ai_move, result, banned)
        await conn.execute(  # 90 天前旧战绩清掉，表不无限涨
            "DELETE FROM game_rps WHERE created_at < now() - interval '90 days'")


async def _ledger_and_panel(user_id: str, user_move: str, ai_move: str, banned: bool) -> None:
    from app.memory import ledger

    detail = (f"用户 {user_id} 猜拳输了（{user_move} vs {ai_move}），"
              f"封号 {BAN_MINUTES} 分钟（登录态保留，重登被 banend 拦截）")
    await ledger.record("system", "game_ban", f"rps 输 → 封禁 {BAN_MINUTES}min",
                        target=user_id, args_summary={"user_move": user_move, "ai_move": ai_move},
                        detail=detail)


# ---------- HTTP ----------

async def ban_status(request: Request):
    """账号封禁状态（覆盖辱骂禁言 + 猜拳封禁，banend 为唯一事实源）。

    前端轮询用：淡悬浮窗展示剩余倒计时。reason 取 PG 审计表最近一条封禁记录，
    查不到就给通用说法——审计缺失不影响状态正确性（状态只看 MySQL banend）。"""
    ident = deps.verify_request(request)
    uid = _uid(ident.user_id)
    remaining = await _ban_remaining_seconds(uid) if uid > 0 else 0
    reason = ""
    if remaining > 0 and _pool is not None:
        try:
            async with _pool.acquire() as conn:
                game_row = await conn.fetchrow(
                    "SELECT created_at FROM game_rps"
                    " WHERE user_id = $1 AND banned ORDER BY created_at DESC LIMIT 1",
                    ident.user_id)
            from app.moderation import _pool as mod_pool

            strike_row = None
            if mod_pool is not None:
                async with mod_pool.acquire() as conn:
                    strike_row = await conn.fetchrow(
                        "SELECT created_at FROM moderation_strikes"
                        " WHERE user_id = $1 AND banned ORDER BY created_at DESC LIMIT 1",
                        ident.user_id)
            if game_row and (not strike_row or game_row["created_at"] > strike_row["created_at"]):
                reason = "猜拳输给了小秋"
            elif strike_row:
                reason = "多次辱骂"
        except Exception:
            log.exception("ban reason lookup failed")
    return _ok({
        "banned": remaining > 0,
        "remaining": remaining,
        "until": (datetime.now(UTC) + timedelta(seconds=remaining)).isoformat() if remaining else None,
        "reason": reason,
    })


async def _change_ban(uid: int, minutes: int) -> None:
    """减刑局对 banend 的调整：正数加刑（banend 必然在未来），负数减刑（clamp 到 NOW，
    剩余不足 5 分钟时赢了等于当场释放）。"""
    from app.tools.forum.sql_templates import execute_write

    if minutes >= 0:
        await execute_write(
            "UPDATE users SET banend = banend + INTERVAL %s MINUTE WHERE userID = %s",
            (minutes, uid))
    else:
        await execute_write(
            "UPDATE users SET banend = GREATEST(NOW(), banend - INTERVAL %s MINUTE)"
            " WHERE userID = %s", (-minutes, uid))
    log.info("sentence %s user %s: %+d min", "extend" if minutes >= 0 else "commute", uid, minutes)


async def _ledger_sentence(user_id: str, user_move: str, ai_move: str,
                           mode: str, remaining_after: int) -> None:
    from app.memory import ledger

    verdict = ("减刑局输了，刑期 +5 分钟" if mode == "extend" else "减刑局赢了，刑期 -5 分钟")
    detail = (f"用户 {user_id} {verdict}（{user_move} vs {ai_move}），"
              f"剩余 {remaining_after} 秒")
    await ledger.record("system", "game_ban", verdict, target=user_id,
                        args_summary={"user_move": user_move, "ai_move": ai_move, "mode": mode},
                        detail=detail)


class GameError(Exception):
    """开局被拒（非法拳/无资格）。"""

    def __init__(self, msg: str, remaining: int = 0):
        super().__init__(msg)
        self.remaining = remaining


async def play_core(user_id: str, move: str, admin_exempt: bool) -> dict:
    """猜拳共同核心：HTTP 端点与 chat 工具（play_rps）都走这里。

    封禁期开放减刑局：赢 -5 分钟（clamp 到 NOW，可当场释放）、平不变、输 +5 分钟
    累计。返回 HTTP data 同构 dict；拒绝时抛 GameError。"""
    move = (move or "").strip().lower()
    if move not in MOVES:
        raise GameError("move 必须是 rock/paper/scissors")

    uid = _uid(user_id)
    if uid <= 0:
        raise GameError("该账号不能参与猜拳")

    remaining = await _ban_remaining_seconds(uid)
    in_sentence = remaining > 0

    ai_move = secrets.choice(MOVES)
    if move == ai_move:
        result = "draw"
    elif _BEATS[move] == ai_move:
        result = "win"
    else:
        result = "lose"

    banned = False
    note = ""
    sentence: dict | None = None
    if in_sentence:
        # 减刑局：admin 不豁免输局加刑（能被封说明账号本身在受罚状态），赢局照常减刑
        if result == "lose":
            await _change_ban(uid, +BAN_MINUTES)
            banned = True
            sentence = {"mode": "extend", "changed_seconds": BAN_MINUTES * 60}
            note = "减刑局输了：刑期 +5 分钟"
        elif result == "win":
            await _change_ban(uid, -BAN_MINUTES)
            sentence = {"mode": "commute", "changed_seconds": BAN_MINUTES * 60}
            note = "减刑局赢了：刑期 -5 分钟"
        else:
            sentence = {"mode": "none", "changed_seconds": 0}
            note = "平局：刑期不变"
    elif result == "lose":
        if admin_exempt:
            note = "管理员豁免：本次免于封号"
        else:
            banned = await _execute_game_ban(uid)
            if banned:
                note = f"封号 {BAN_MINUTES} 分钟已执行（当前登录态保留，期间无法重新登录）"
                await _ledger_and_panel(user_id, move, ai_move, banned)

    remaining_after = await _ban_remaining_seconds(uid)
    if sentence and sentence["mode"] != "none":
        await _ledger_sentence(user_id, move, ai_move, sentence["mode"], remaining_after)
    if sentence:
        sentence["remaining_after"] = remaining_after

    await _record(user_id, move, ai_move, result, banned)
    return {
        "user_move": move, "ai_move": ai_move, "result": result,
        "note": note,
        "sentence": sentence,
        "remaining_after": remaining_after,
        "ban": ({"banned": banned, "seconds": BAN_MINUTES * 60,
                 "until": (datetime.now(UTC) + timedelta(seconds=remaining_after)).isoformat()}
                if banned else None),
    }


async def play(request: Request):
    ident = deps.verify_request(request)
    body = await request.json()
    try:
        data = await play_core(
            ident.user_id, str(body.get("move", "")),
            admin_exempt=ident.role == "admin" or ident.user_id.startswith("a"))
    except GameError as e:
        raise HTTPException(status_code=423 if e.remaining else 422, detail=str(e))
    return _ok(data)


async def stats(request: Request):
    ident = deps.verify_request(request)
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE result='win') AS wins,"
            " count(*) FILTER (WHERE result='lose') AS losses,"
            " count(*) FILTER (WHERE result='draw') AS draws,"
            " count(*) FILTER (WHERE banned) AS bans"
            " FROM game_rps WHERE user_id = $1", ident.user_id)
        recent = await conn.fetch(
            "SELECT user_move, ai_move, result, banned, created_at FROM game_rps"
            " WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10", ident.user_id)
        total = await conn.fetchval("SELECT count(*) FROM game_rps")
        total_bans = await conn.fetchval("SELECT count(*) FROM game_rps WHERE banned")
    return _ok({
        "me": {"wins": row["wins"], "losses": row["losses"], "draws": row["draws"],
               "bans": row["bans"]},
        "global": {"games": total, "bans": total_bans},
        "recent": [{"user_move": r["user_move"], "ai_move": r["ai_move"],
                    "result": r["result"], "banned": r["banned"],
                    "created_at": r["created_at"].isoformat()} for r in recent],
    })
