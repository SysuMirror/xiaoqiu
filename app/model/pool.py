"""多端点模型池：有序端点、单调用重试、跨端点回退、连续失败熔断。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

from app.config import AppConfig, EndpointCfg

log = logging.getLogger(__name__)


class AllEndpointsFailedError(RuntimeError):
    pass


@dataclass
class _Breaker:
    threshold: int
    cooldown: float
    failures: int = 0
    opened_at: float = 0.0

    def is_open(self) -> bool:
        if self.failures < self.threshold:
            return False
        # 冷却期满 → 半开放行一次探测；探测失败 record_failure 会立即重置
        # opened_at（failures 仍 ≥ 阈值）→ 重新打开，不会连吃三次慢探测
        return time.monotonic() - self.opened_at < self.cooldown

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            log.warning("breaker opened after %d failures", self.failures)


@dataclass
class _Endpoint:
    cfg: EndpointCfg
    breaker: _Breaker
    client: ChatOpenAI


@dataclass
class UsageLedger:
    """累计 token 记账（测试预算用）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def add(self, usage) -> None:
        async with self._lock:
            self.calls += 1
            if usage:
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def summary(self) -> str:
        return (
            f"calls={self.calls} prompt={self.prompt_tokens} "
            f"completion={self.completion_tokens} total={self.total_tokens}"
        )


LEDGER = UsageLedger()


class _CountingChatOpenAI(ChatOpenAI):
    """包一层 usage 记账，其余行为不变。"""

    ledger: UsageLedger | None = None

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if self.ledger is not None and result.generations:
            msg = result.generations[0].message
            usage_meta = getattr(msg, "usage_metadata", None) or {}
            resp = getattr(msg, "response_metadata", None) or {}
            token_usage = resp.get("token_usage") or {}
            await self.ledger.add(
                type("U", (), {
                    "prompt_tokens": usage_meta.get("input_tokens", 0)
                    or token_usage.get("prompt_tokens", 0),
                    "completion_tokens": usage_meta.get("output_tokens", 0)
                    or token_usage.get("completion_tokens", 0),
                })()
            )
        return result


class ModelPool:
    """端点池：有序端点、腿间回退、熔断。构造走 from_config（chat）或直接传
    EndpointCfg 列表（ops，端点由 OpsLlmCfg.as_endpoints() 转换）——两面同一套
    池化容错，不再各写一份换腿逻辑。"""

    def __init__(self, endpoints: list[EndpointCfg], *, timeout: int, retry: int = 1,
                 breaker_threshold: int = 3, breaker_cooldown: int = 60,
                 ledger: UsageLedger | None = None):
        self._timeout = timeout
        self._retry = retry
        self._ledger = ledger
        # 最近一次成功调用的端点名（多 run 并发下是近似值，仅供 done{model} 展示）
        self.last_served = ""
        self._endpoints: list[_Endpoint] = [
            _Endpoint(
                cfg=ep,
                breaker=_Breaker(threshold=breaker_threshold, cooldown=breaker_cooldown),
                client=self._build_client(ep),
            )
            for ep in endpoints
        ]

    @classmethod
    def from_config(cls, cfg: AppConfig, ledger: UsageLedger | None = None) -> "ModelPool":
        """chat 面构造入口：端点/超时/重试/熔断全部来自 llm 配置段。"""
        llm = cfg.llm
        return cls(llm.endpoints, timeout=llm.request_timeout_seconds,
                   retry=llm.retry_attempts,
                   breaker_threshold=llm.breaker_failure_threshold,
                   breaker_cooldown=llm.breaker_cooldown_seconds, ledger=ledger)

    def _build_client(self, ep: EndpointCfg) -> ChatOpenAI:
        import os

        key = os.environ.get(ep.api_key_env, "")
        if not key:
            raise RuntimeError(f"{ep.api_key_env} 未配置")
        return _CountingChatOpenAI(
            model=ep.model,
            base_url=ep.base_url,
            api_key=key,
            timeout=self._timeout,
            max_retries=0,  # 重试由池统一管理
            extra_body=ep.extra_body or None,
            ledger=self.ledger,
        )

    @property
    def ledger(self) -> UsageLedger:
        return self._ledger or LEDGER

    def primary(self) -> BaseChatModel:
        return self._endpoints[0].client

    def primary_name(self) -> str:
        return self._endpoints[0].cfg.name

    async def ainvoke(self, messages: list[BaseMessage]) -> BaseMessage:
        """按端点顺序尝试：单端点内 retry，端点间回退，熔断跳过。"""
        errors: list[str] = []
        for ep in self._endpoints:
            if ep.breaker.is_open():
                errors.append(f"{ep.cfg.name}: breaker open")
                continue
            for attempt in range(1, self._retry + 1):
                try:
                    msg = await ep.client.ainvoke(messages)
                    ep.breaker.record_success()
                    return msg
                except Exception as e:  # noqa: BLE001 —— 模型网关错误形态不可枚举
                    ep.breaker.record_failure()
                    errors.append(f"{ep.cfg.name} attempt {attempt}: {type(e).__name__}: {e}")
                    if attempt < self._retry:
                        await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            log.error("endpoint %s exhausted, falling back", ep.cfg.name)
        raise AllEndpointsFailedError("; ".join(errors))

    async def ainvoke_with_fallback(self, bound_primary, tools, messages,
                                    config=None) -> BaseMessage:
        """chat 图的池化调用：先走已绑定的主端点（保持既有流式路径原样），
        失败按池顺序回退其余端点（bind_tools 后调用）。

        背景：图此前直接 primary().bind_tools().ainvoke()，绕过了池的全部
        回退逻辑——主网关 502 时智谱回落端点从未被尝试，用户直接收到
        50000（2026-10-07 线上事故）。主端点一次即让（上游自身有重试），
        熔断打开时跳过主端点直接走回落。
        """
        errors: list[str] = []
        first = self._endpoints[0]
        if first.breaker.is_open():
            errors.append(f"{first.cfg.name}: breaker open")
        else:
            try:
                msg = await bound_primary.ainvoke(messages, config=config)
                first.breaker.record_success()
                self.last_served = first.cfg.name
                return msg
            except Exception as e:  # noqa: BLE001
                first.breaker.record_failure()
                errors.append(f"{first.cfg.name}: {type(e).__name__}: {e}")
                log.error("primary endpoint failed, trying fallbacks: %s", str(e)[:200])
        for ep in self._endpoints[1:]:
            if ep.breaker.is_open():
                errors.append(f"{ep.cfg.name}: breaker open")
                continue
            client = ep.client.bind_tools(tools) if tools else ep.client
            for attempt in range(1, self._retry + 1):
                try:
                    msg = await client.ainvoke(messages, config=config)
                    ep.breaker.record_success()
                    self.last_served = ep.cfg.name
                    if errors:
                        log.warning("served by fallback endpoint %s (primary failed: %s)",
                                    ep.cfg.name, errors[-1][:120])
                    return msg
                except Exception as e:  # noqa: BLE001
                    ep.breaker.record_failure()
                    errors.append(f"{ep.cfg.name} attempt {attempt}: {type(e).__name__}: {e}")
                    if attempt < self._retry:
                        await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            log.error("endpoint %s exhausted", ep.cfg.name)
        raise AllEndpointsFailedError("; ".join(errors))

    async def astream_with_fallback(self, bound_primary, tools, messages, config=None):
        """ops 图的池化流式调用：主腿 astream 一次让，零 chunk 失败按池顺序回退
        其余端点（bind_tools 后调用，3 次指数退避），全挂抛 AllEndpointsFailedError。

        与 ainvoke_with_fallback 同构（熔断/退避/记账复用）。语义边界：已吐出
        chunk 后再失败不换腿——备腿续流会把「半截+全文」拼在一起（重复文本），
        与平台网关同款「no fallback past this point」，直接上抛由上层按错误码收口。
        """
        errors: list[str] = []
        first = self._endpoints[0]
        if first.breaker.is_open():
            errors.append(f"{first.cfg.name}: breaker open")
        else:
            chunks = 0
            try:
                async for chunk in bound_primary.astream(messages, config=config):
                    chunks += 1
                    yield chunk
                first.breaker.record_success()
                self.last_served = first.cfg.name
                return
            except Exception as e:  # noqa: BLE001
                first.breaker.record_failure()
                if chunks:
                    raise
                errors.append(f"{first.cfg.name}: {type(e).__name__}: {e}")
                log.error("primary endpoint failed, trying fallbacks: %s", str(e)[:200])
        for ep in self._endpoints[1:]:
            if ep.breaker.is_open():
                errors.append(f"{ep.cfg.name}: breaker open")
                continue
            client = ep.client.bind_tools(tools) if tools else ep.client
            for attempt in range(1, self._retry + 1):
                chunks = 0
                try:
                    async for chunk in client.astream(messages, config=config):
                        chunks += 1
                        yield chunk
                    ep.breaker.record_success()
                    self.last_served = ep.cfg.name
                    if errors:
                        log.warning("served by fallback endpoint %s (primary failed: %s)",
                                    ep.cfg.name, errors[-1][:120])
                    return
                except Exception as e:  # noqa: BLE001
                    ep.breaker.record_failure()
                    if chunks:
                        raise
                    errors.append(f"{ep.cfg.name} attempt {attempt}: {type(e).__name__}: {e}")
                    if attempt < self._retry:
                        await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            log.error("endpoint %s exhausted", ep.cfg.name)
        raise AllEndpointsFailedError("; ".join(errors))

    async def ainvoke_no_tools(self, messages, config=None) -> BaseMessage:
        """收口通路：与 ainvoke_with_fallback 同池序同熔断，但**不绑工具**。

        工具轮预算到顶后的硬停手段——弱模型对「立即停止调用工具」的劝告会无视
        （2026-10-10 glm-4.5-air sql_read 75 连发实录），不绑工具它在协议层
        就发不出 tool_calls。
        """
        errors: list[str] = []
        for ep in self._endpoints:
            if ep.breaker.is_open():
                errors.append(f"{ep.cfg.name}: breaker open")
                continue
            try:
                msg = await ep.client.ainvoke(messages, config=config)
                ep.breaker.record_success()
                return msg
            except Exception as e:  # noqa: BLE001
                ep.breaker.record_failure()
                errors.append(f"{ep.cfg.name}: {type(e).__name__}: {e}")
        raise AllEndpointsFailedError("; ".join(errors))

    def breaker_status(self) -> dict[str, str]:
        return {e.cfg.name: "open" if e.breaker.is_open() else "closed" for e in self._endpoints}

    def chain_status(self) -> list[dict]:
        """有序端点身份：[{name, model, breaker}]。给 /health/ready 与 ops 排障用——
        熔断开合只说明通断，模型是谁（含是否强制思考）才是延迟归因的第一事实。"""
        return [{"name": e.cfg.name, "model": getattr(e.cfg, "model", "?"),
                 "breaker": "open" if e.breaker.is_open() else "closed"}
                for e in self._endpoints]
