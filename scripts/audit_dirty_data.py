#!/usr/bin/env python
"""生产库脏数据侦查（只读）：双采集器交错帧 / 测试注入残留 / 指纹格式分裂 / 超期桶。"""
import asyncio
import os
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncpg


async def main():
    conn = await asyncpg.connect(os.environ.get("AGENT_PG_DSN", "postgresql://agent:agent@127.0.0.1:5433/sse_agent"))
    q = lambda s: conn.fetch(s)  # noqa: E731

    print("① 双采集器交错检测（正常=单采集器每分钟 1-2 帧；本地 dev 经隧道同库写 → 同分钟 4+ 帧）")
    frames = await q("""SELECT date_trunc('minute', ts) m, count(DISTINCT date_trunc('second', ts)) f
                        FROM go_route_stats WHERE ts > now() - interval '2 hours'
                        GROUP BY 1 ORDER BY 1 DESC LIMIT 6""")
    for f in frames:
        print("   ", f["m"].strftime("%m-%d %H:%M"), f["f"], "帧")
    dense = await q("""SELECT count(*) FROM (
        SELECT date_trunc('minute', ts) FROM go_route_stats
        WHERE ts > now() - interval '24 hours'
        GROUP BY 1 HAVING count(DISTINCT date_trunc('second', ts)) > 3) t""")
    print("    24h 内 >3帧/分钟:", dense[0][0], "个分钟")

    print("② 指纹格式分裂（route_tpl 上线后 :param 与旧 {param} 并存）")
    brace = await q("SELECT fingerprint, method, route, status, count FROM error_fingerprints WHERE route LIKE '%{%' ORDER BY count DESC")
    colon = await q("SELECT count(*) FROM error_fingerprints WHERE route LIKE '%:%' AND route NOT LIKE '%{%'")
    print(f"    {{param}} 旧格式: {len(brace)} 条 · :param 新格式: {colon[0][0]} 条")
    for r in brace[:6]:
        print("    -", r["method"], r["route"], r["status"], "x", r["count"])

    print("③ 测试注入残留")
    tr = await q("SELECT DISTINCT route FROM go_route_stats WHERE route ~* '(test|/__|mock)' LIMIT 8")
    print("    测试嫌疑路由:", [r[0] for r in tr])
    pb = await q("SELECT name FROM ops_playbooks WHERE name ~* '(i4[0-9]|test)'")
    print("    测试剧本:", [r[0] for r in pb])
    ann = await q("SELECT count(*) FROM annotations WHERE title ~* '(test|测试注入)' OR detail ~* 'i2[0-9]_'")
    print("    测试嫌疑标注:", ann[0][0])

    print("④ 采集帧异常")
    fut = await q("SELECT count(*) FROM go_route_stats WHERE ts > now() + interval '5 minutes'")
    neg = await q("SELECT count(*) FROM go_route_rollup_5m WHERE requests < 0")
    old_bucket = await q("SELECT count(*) FROM go_route_rollup_5m WHERE bucket < now() - interval '8 days'")
    stats_old = await q("SELECT min(ts), max(ts), count(*) FROM go_route_stats")
    print(f"    未来帧: {fut[0][0]} · 负桶: {neg[0][0]} · 超8天桶: {old_bucket[0][0]}")
    print(f"    go_route_stats 时间范围: {stats_old[0][0]} ~ {stats_old[0][1]} 共 {stats_old[0][2]} 行")

    print("⑤ error_samples 测试样本")
    es = await q("SELECT count(*) FROM error_samples WHERE path ~* '(test|/__|mock)'")
    print("    测试嫌疑样本:", es[0][0])
    await conn.close()


asyncio.run(main())
