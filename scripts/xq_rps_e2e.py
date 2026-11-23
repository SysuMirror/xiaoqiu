"""猜拳 E2E：假用户 u95270 走 /api/v1/agent/game/rps 全链路，结束清场。

覆盖：鉴权 401、非法拳 422、正常出拳契约（win/lose/draw + ban 字段）、连打 30 局
随机性 sanity、stats 计数自洽。假用户不在 users 表，封号 UPDATE 0 行无伤。
用法: XQ_E2E_BASE=https://ssemarket.cn .venv/Scripts/python scripts/xq_rps_e2e.py
"""
from __future__ import annotations

import os
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.api import deps  # noqa: E402

BASE = os.environ.get("XQ_E2E_BASE", "http://127.0.0.1:8100")
U1 = "u95270"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def hdr(uid: str) -> dict:
    return {"Authorization": f"Bearer {deps.mint_token(uid, 'user')}",
            "Content-Type": "application/json"}


def call(method: str, path: str, uid: str | None = None, body: dict | None = None):
    with httpx.Client(timeout=30, trust_env=False) as hc:
        r = hc.request(method, f"{BASE}{path}",
                       headers=hdr(uid) if uid else {"Content-Type": "application/json"},
                       json=body)
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, {}


def play(move: str, uid: str = U1):
    return call("POST", "/api/v1/agent/game/rps", uid, {"move": move})


def main():
    # 1. 未鉴权 401
    status, _ = call("POST", "/api/v1/agent/game/rps", None, {"move": "rock"})
    check("no-auth 401", status == 401, f"status={status}")

    # 2. 非法拳 422
    status, body = play("bomb")
    check("invalid move 422", status == 422, f"status={status}")

    # 3. 合法出拳契约
    outcomes: dict[str, int] = {}
    ai_moves: set[str] = set()
    for _ in range(30):
        for m in ("rock", "paper", "scissors"):
            status, body = play(m)
            d = body.get("data", {})
            ok = (status == 200 and body.get("code") == 0
                  and d.get("user_move") == m
                  and d.get("ai_move") in ("rock", "paper", "scissors")
                  and d.get("result") in ("win", "lose", "draw")
                  and ("ban" in d))
            if not ok:
                check("play contract", False, f"status={status} body={str(body)[:200]}")
                return
            outcomes[d["result"]] = outcomes.get(d["result"], 0) + 1
            ai_moves.add(d["ai_move"])
    check("play contract 90 局", True, f"outcomes={outcomes}")

    # 4. 随机性 sanity：90 局小秋不该只出一种拳
    check("ai randomness", len(ai_moves) == 3, f"ai_moves={sorted(ai_moves)}")
    check("three outcomes seen", len(outcomes) == 3, str(outcomes))

    # 5. stats 自洽：总数 = 90 + 此前残留
    status, body = call("GET", "/api/v1/agent/game/rps/stats", U1)
    me = body.get("data", {}).get("me", {})
    total_me = me.get("wins", 0) + me.get("losses", 0) + me.get("draws", 0)
    check("stats 200 + 计数 >= 90", status == 200 and total_me >= 90,
          f"status={status} me={me}")
    g = body.get("data", {}).get("global", {})
    check("stats global 字段", "games" in g and "bans" in g, str(g))

    # 6. 对话触发：聊天里说猜拳，LLM 应调 play_rps 工具并宣布结果
    ok, detail = chat_game(U1, "来玩剪刀石头布，我出石头！")
    check("chat 触发猜拳", ok, detail)


def chat_game(uid: str, text: str) -> tuple[bool, str]:
    import json as _json
    import uuid as _uuid

    session_id = f"xq_e2e_rps_{_uuid.uuid4().hex[:8]}"
    finals, statuses = [], []
    with httpx.Client(timeout=120, trust_env=False) as hc, \
            hc.stream("POST", f"{BASE}/api/v1/agent/task/sse", headers=hdr(uid),
                      json={"task": text, "session_id": session_id}) as r:
        if r.status_code != 200:
            return False, f"sse status={r.status_code}"
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                try:
                    d = _json.loads(line.split(":", 1)[1].strip())
                except _json.JSONDecodeError:
                    continue
                if event == "content" and d.get("mode") == "final":
                    finals.append(d.get("text", ""))
                elif event == "status":
                    statuses.append(d)
    final = finals[-1] if finals else ""
    tool_fired = any("play_rps" in _json.dumps(s, ensure_ascii=False) for s in statuses)
    game_words = ("石头", "剪刀", "布", "猜拳", "奖励", "封")
    looks_game = any(w in final for w in game_words)
    return bool(final and (tool_fired or looks_game)), \
        f"tool_fired={tool_fired} final={final[:80]!r}"


def cleanup():
    try:
        import asyncio

        async def _del():
            import asyncpg

            from app.config import get_config

            conn = await asyncpg.connect(get_config().pg_dsn)
            try:
                n = await conn.fetchval("DELETE FROM game_rps WHERE user_id = $1", U1)
                print(f"cleanup: game_rps deleted {n} rows for {U1}")
                # 聊天测试产生的会话与 checkpoint
                await conn.execute("DELETE FROM threads WHERE user_id = $1", U1)
                await conn.execute("DELETE FROM checkpoints WHERE thread_id LIKE 'xq_e2e_rps%'")
                await conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id LIKE 'xq_e2e_rps%'")
                await conn.execute("DELETE FROM checkpoint_writes WHERE thread_id LIKE 'xq_e2e_rps%'")
                await conn.execute("DELETE FROM store WHERE prefix LIKE '%u95270%'")
                print("cleanup: threads/checkpoints/store cleared")
            finally:
                await conn.close()

        asyncio.run(_del())
    except Exception as e:  # noqa: BLE001
        print(f"cleanup skipped: {e}")


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
        failed = [r for r in results if not r[1]]
        print(f"\n{len(results) - len(failed)}/{len(results)} passed")
        sys.exit(1 if failed else 0)
