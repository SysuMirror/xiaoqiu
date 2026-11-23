"""pg_query 本地冒烟：经 SSH 隧道连主库（.env 的 AGENT_PG_DSN，隧道见 dev_tunnel.sh），
跑首 token 排查的典型查询 + 只读守卫演示，直接看工具真实输出。

用法：.venv/Scripts/python.exe scripts/dev_pg_query_smoke.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.win_compat import setup_event_loop

setup_event_loop()

import app.ops.pgquery as pgq

QUERIES = [
    ("守卫演示：写操作拒绝（不触库）", "UPDATE runs SET status='failed'"),
    ("库身份", "SHOW server_version"),
    ("近 24h 首 token 分布（按模型：n/p50/p95/avg/max）",
     "SELECT model, count(*) AS n, percentile_disc(0.5) WITHIN GROUP (ORDER BY first_token_ms) AS p50_ms, "
     "percentile_disc(0.95) WITHIN GROUP (ORDER BY first_token_ms) AS p95_ms, "
     "round(avg(first_token_ms)) AS avg_ms, max(first_token_ms) AS max_ms "
     "FROM runs WHERE created_at > now() - interval '24 hours' AND first_token_ms IS NOT NULL "
     "GROUP BY 1 ORDER BY n DESC"),
    ("近 48h 最慢 10 条真实请求",
     "SELECT run_id, user_id, model, first_token_ms AS ttft_ms, tokens_total, "
     "to_char(created_at, 'MM-DD HH24:MI') AS at "
     "FROM runs WHERE first_token_ms IS NOT NULL AND created_at > now() - interval '48 hours' "
     "ORDER BY first_token_ms DESC LIMIT 10"),
    ("近 48h 按小时趋势（n/avg/max）",
     "SELECT to_char(date_trunc('hour', created_at), 'MM-DD HH24:MI') AS hour, count(*) AS n, "
     "round(avg(first_token_ms)) AS avg_ms, max(first_token_ms) AS max_ms "
     "FROM runs WHERE first_token_ms IS NOT NULL AND created_at > now() - interval '48 hours' "
     "GROUP BY 1 ORDER BY 1 DESC LIMIT 24"),
]


async def main() -> None:
    for title, q in QUERIES:
        print(f"\n===== {title} =====")
        print(await pgq.run_query(q))
    await pgq.close()


if __name__ == "__main__":
    asyncio.run(main())
