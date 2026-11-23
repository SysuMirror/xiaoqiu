"""模型池冒烟测试：正常调用 + usage 记账。烧 token，勿频繁跑。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from langchain_core.messages import HumanMessage

from app.config import get_config
from app.model.pool import ModelPool


async def main() -> None:
    cfg = get_config()
    pool = ModelPool.from_config(cfg)
    reply = await pool.ainvoke([HumanMessage(content="只回复两个字：收到")])
    text = reply.text() if hasattr(reply, "text") else str(reply.content)
    print(f"reply: {text[:50]}")
    print(f"ledger: {pool.ledger.summary()}")
    print(f"breakers: {pool.breaker_status()}")


if __name__ == "__main__":
    asyncio.run(main())
