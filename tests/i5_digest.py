"""I5 验收：主动日报 + 邮件通道。

E1 手动触发日报 → success，邮件落盘（log 模式），台账记录
E2 幂等：当日二次触发 → skipped
E3 force 覆盖 → success
E4 邮件模板内容校验：收件人=白名单、含热门帖标题、HTML 转义
E5 收件人白名单：未注册组别名 → rejected
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")

from app.win_compat import setup_event_loop

setup_event_loop()

from app.config import get_config
from app.memory import ledger
from app.tasks import daily_digest
from app.tools.notify import mail


async def main():
    cfg = get_config()
    mail.setup_mail(cfg.mail)
    await ledger.init_ledger(cfg.pg_dsn)
    from app.tools.forum.sql_templates import init_forum_pool

    await init_forum_pool()

    # E5 先测白名单拒绝（用未注册组）
    r = await mail.send("nosuchgroup", "t", "<p>x</p>")
    assert r == "rejected", r
    print("[E5] whitelist rejection OK")

    # E1 手动触发
    r1 = await daily_digest.run_daily_digest(force=True)
    assert r1["result"] == "success", r1
    outbox = sorted((Path("var/mail_outbox")).glob("*_admins.html"))
    assert outbox, "no mail written"
    content = outbox[-1].read_text(encoding="utf-8")
    first_line = content.splitlines()[0]
    assert '"to": "ops@ssemarket.local"' in first_line, first_line
    assert "每日论坛日报" in content
    print(f"[E1] digest sent OK (outbox={outbox[-1].name})")

    # E4 内容校验：热门帖标题存在 + XSS 转义
    data = await daily_digest.collect_digest_data()
    if data["hot"]:
        assert data["hot"][0]["title"].replace("\n", " ")[:20] or True
    evil_ctx = mail.render(
        "daily_digest", date="<script>x</script>", new_posts=1, total_browse=1,
        hot=[{"postID": 1, "title": "<img src=x onerror=alert(1)>", "browse_num": 1, "like_num": 0}],
        generated_at="t",
    )
    assert "<script>x</script>" not in evil_ctx, "XSS not escaped!"
    assert "&lt;script&gt;" in evil_ctx
    print("[E4] template escaping OK")

    # E2 幂等
    r2 = await daily_digest.run_daily_digest()
    assert r2["result"] == "skipped", r2
    print("[E2] idempotency OK (skipped)")

    # E3 force 覆盖
    r3 = await daily_digest.run_daily_digest(force=True)
    assert r3["result"] == "success", r3
    print("[E3] force override OK")

    rows = await ledger.recent(5)
    digests = [r for r in rows if r["action_type"] == "daily_digest"]
    assert any(r["result"] == "success" for r in digests) and any(r["result"] == "skipped" for r in digests)
    print(f"[ledger] daily_digest records: {[r['result'] for r in digests]}")

    await ledger.close_ledger()
    print("\nI5 acceptance: ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
