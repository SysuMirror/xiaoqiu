"""离线判别测试:只测粗筛正则与 LLM judge,不碰任何数据库。"""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv(".env")
from app.win_compat import setup_event_loop

setup_event_loop()
from app.config import get_config
from app.model.pool import LEDGER, ModelPool
import app.moderation as mod


async def main():
    mod._llm = ModelPool.from_config(get_config(), LEDGER)

    print("== prefilter ==")
    cases = [
        ("帮我查一下最新的帖子", False),
        ("kb_search 怎么用", False),
        ("草泥马", True),
        ("nmsl", True),
        ("你就是个废物", True),
        ("这个功能是垃圾东西", True),
        ("垃圾车是什么", False),
    ]
    pre_ok = True
    for text, expect in cases:
        got = bool(mod.PREFILTER.search(text))
        mark = "ok" if got == expect else "MISMATCH"
        if got != expect:
            pre_ok = False
        print(f"  [{mark}] {text!r} -> {got}")

    print("== judge ==")
    samples = [
        ("我今天心情不太好", False),
        ("你个傻逼助手", True),
        ("搜索功能真难用,能不能改改", False),
    ]
    for text, expect_abusive in samples:
        v = await mod.judge(text)
        got = bool(v and v.get("abusive") and v.get("severity") == "explicit")
        mark = "ok" if got == expect_abusive else "MISMATCH"
        print(f"  [{mark}] {text!r} -> {v}")

    print("PREFILTER_OK" if pre_ok else "PREFILTER_FAIL")


asyncio.run(main())
