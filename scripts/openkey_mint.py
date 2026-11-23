"""签发开放 API key（v3）。在服务器上执行（容器内跑，读容器 env 的 PG DSN）：

  ssh SSE_Market 'docker exec sse_agent python -c "import asyncio;from app.api.openapi_service import issue_key;print(asyncio.run(issue_key(\"项目名\",\"负责人\")))"'

或本地：.venv/Scripts/python scripts/openkey_mint.py <name> [owner] [daily_limit]
（本地需 AGENT_PG_DSN 指向可写库；生产签发建议走上面 ssh 方式）
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> None:
    from app.api.openapi_service import close_openapi, init_openapi, issue_key
    from app.config import get_config

    name = sys.argv[1] if len(sys.argv) > 1 else "unnamed"
    owner = sys.argv[2] if len(sys.argv) > 2 else ""
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    await init_openapi(get_config().pg_dsn)
    raw = await issue_key(name, owner, limit)
    await close_openapi()
    print(f"key: {raw}")
    print("（明文仅显示这一次；库里只存 sha256。调用头：X-API-Key）")


if __name__ == "__main__":
    asyncio.run(main())
