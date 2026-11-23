"""chat 池回落单测（ainvoke_with_fallback + 熔断半开语义）。

2026-10-07 线上事故回归：主网关 502 → 图直连 primary 绕过回落 → 50000。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage

from app.model.pool import AllEndpointsFailedError, ModelPool, _Breaker


class FakeClient:
    def __init__(self, behavior):
        self.behavior = behavior  # Exception | AIMessage | callable(msgs)
        self.calls = 0
        self.bound = None
        self._clone = None

    def bind_tools(self, tools):
        clone = FakeClient(self.behavior)
        clone.calls = self.calls
        clone.bound = list(tools)
        self._clone = clone
        return clone

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if self._clone is not None:
            self._clone.calls += 1
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior(messages)
        return self.behavior


def _mk_pool(monkeypatch, behaviors, threshold=3, retry=1):
    """两/三端点池，client 全部替换为 FakeClient（不碰 env/网络）。"""
    eps = []
    for i, b in enumerate(behaviors):
        eps.append(SimpleNamespace(
            cfg=SimpleNamespace(name=f"ep{i}"),
            breaker=_Breaker(threshold=threshold, cooldown=60.0),
            client=FakeClient(b),
        ))
    pool = ModelPool.__new__(ModelPool)
    pool._cfg = None
    pool._timeout = 5
    pool._retry = retry
    pool._ledger = None
    pool._endpoints = eps
    return pool


def test_primary_failure_falls_back(monkeypatch):
    pool = _mk_pool(monkeypatch, [
        RuntimeError("502 upstream_failed"),
        AIMessage("智谱兜底"),
    ])
    bound = pool._endpoints[0].client.bind_tools(["t1"])
    msg = asyncio.run(pool.ainvoke_with_fallback(bound, ["t1"], [Human("hi")]))
    assert msg.content == "智谱兜底"
    assert pool._endpoints[0].breaker.failures == 1


def test_all_fail_raises_aggregated(monkeypatch):
    pool = _mk_pool(monkeypatch, [
        RuntimeError("502 a"), RuntimeError("502 b"),
    ], retry=2)
    bound = pool._endpoints[0].client.bind_tools([])
    try:
        asyncio.run(pool.ainvoke_with_fallback(bound, [], [Human("hi")]))
        assert False
    except AllEndpointsFailedError as e:
        assert "ep0" in str(e) and "ep1" in str(e) and "502 b" in str(e)


def test_breaker_open_skips_primary(monkeypatch):
    pool = _mk_pool(monkeypatch, [
        RuntimeError("dead"), AIMessage("fallback"),
    ])
    b = pool._endpoints[0].breaker
    for _ in range(3):
        b.record_failure()
    assert b.is_open()
    bound = pool._endpoints[0].client.bind_tools([])
    t0 = asyncio.get_event_loop
    msg = asyncio.run(pool.ainvoke_with_fallback(bound, [], [Human("hi")]))
    assert msg.content == "fallback"
    # 主端点被熔断跳过：bind 前后调用计数都是 0
    assert pool._endpoints[0].client.calls == 0


def test_halfopen_probe_failure_reopens_immediately():
    """半开探测失败必须立刻重开（否则冷却期后连吃 threshold 次慢请求）。"""
    b = _Breaker(threshold=3, cooldown=0.05)
    for _ in range(3):
        b.record_failure()
    assert b.is_open()
    import time
    time.sleep(0.06)
    assert not b.is_open()          # 半开放行
    b.record_failure()              # 探测失败
    assert b.is_open(), "探测失败后应立即重开，不应再放行"


def Human(text):
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=text)
