"""Windows 兼容：SelectorEventLoop（psycopg async 必需）+ 本机地址绕过系统代理。

Windows 上 urllib.getproxies() 会读注册表里的系统代理（Clash 等），
httpx trust_env=True 会把 127.0.0.1 的请求也发给代理 → 502。
"""
from __future__ import annotations

import asyncio
import os
import sys


def setup_event_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    no_proxy = os.environ.get("NO_PROXY", "")
    if "127.0.0.1" not in no_proxy:
        os.environ["NO_PROXY"] = (no_proxy + "," if no_proxy else "") + "localhost,127.0.0.1"
