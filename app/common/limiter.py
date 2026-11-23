"""通用滑动窗口限流器（M3 收敛：全仓库唯一实现，见 docs/ASSET_INVENTORY.md）。

并发说明：FastAPI 单进程事件循环内同步调用（无 await 点），纯同步实现
与被替换的三处原语义一致，无并发竞争。
时钟注入用于测试；运行时用 monotonic（免疫系统时钟跳变，语义优于被替换
处混用的 time.time）。
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """per-key 滑动窗口计数器。

    allow(key, limit, window_s) -> (ok, retry_after_s)
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._windows: dict[str, deque] = {}
        self._clock = clock

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        now = self._clock()
        q = self._windows.setdefault(key, deque())
        while q and now - q[0] >= window_s:
            q.popleft()
        if len(q) >= max(1, limit):
            retry = max(1, int(window_s - (now - q[0])) + 1)
            return False, retry
        q.append(now)
        return True, 0
