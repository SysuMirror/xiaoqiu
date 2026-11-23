"""M3 通用限流器单测（app/common/limiter.py）。"""
from app.common.limiter import SlidingWindowLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_allows_within_limit():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    for _ in range(5):
        ok, retry = lim.allow("k", 5, window_s=60)
        assert ok and retry == 0


def test_blocks_over_limit_with_retry():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        lim.allow("k", 3, window_s=60)
    ok, retry = lim.allow("k", 3, window_s=60)
    assert not ok and retry >= 1


def test_window_slides():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    for _ in range(3):
        lim.allow("k", 3, window_s=60)
    clock.t += 61  # 窗口整体滑出
    ok, _ = lim.allow("k", 3, window_s=60)
    assert ok


def test_keys_are_isolated():
    lim = SlidingWindowLimiter()
    for _ in range(3):
        lim.allow("a", 3)
    ok, _ = lim.allow("b", 3)
    assert ok, "不同 key 互不影响（ops:/chat:/openapi: 前缀隔离语义）"


def test_partial_window_expiry():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    lim.allow("k", 2, window_s=60)
    clock.t += 30
    lim.allow("k", 2, window_s=60)
    ok, _ = lim.allow("k", 2, window_s=60)
    assert not ok
    clock.t += 31  # 第一条滑出，第二条还在
    ok, _ = lim.allow("k", 2, window_s=60)
    assert ok
