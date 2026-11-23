"""Skein 调用留痕回调：把每一次 LangChain 模型调用回报给控制面。

走雁阵的公共 API（``POST /api/v1/llm/calls``），认证用助手凭证 ``SKEIN_ASSISTANT_KEY``；
没配这个环境变量时 trace_patch 不会挂这个 handler，本地开发零开销。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackHandler

log = logging.getLogger("sse-agent")

MAX_TEXT = 16000


def _clip(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:MAX_TEXT]
    except (TypeError, ValueError):
        return str(value)[:MAX_TEXT]


def _messages(messages: Any) -> str:
    lines: list[str] = []
    for batch in messages or []:
        for message in batch or []:
            role = getattr(message, "type", "") or getattr(message, "role", "")
            content = getattr(message, "content", "")
            lines.append(f"{role}: {_clip(content)}")
    return "\n".join(lines)[:MAX_TEXT]


def _usage_value(usage: Any, *names: str) -> int:
    if hasattr(usage, "model_dump"):
        try:
            usage = usage.model_dump()
        except Exception:  # noqa: BLE001
            pass
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


def _model_name(serialized: Any, metadata: dict | None) -> str:
    if isinstance(serialized, dict):
        kwargs = serialized.get("kwargs") or {}
        for key in ("model", "model_name", "model_id"):
            value = kwargs.get(key) if isinstance(kwargs, dict) else None
            if value:
                return str(value)
        if serialized.get("name"):
            return str(serialized["name"])
    if metadata:
        return str(metadata.get("ls_model_name") or "")
    return ""


class SkeinTraceCallback(AsyncCallbackHandler):
    def __init__(
        self,
        base_url: str,
        key: str,
        session_id: str = "",
        user_id: str = "",
        source: str = "chat",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.session_id = (session_id or "")[:128]
        self.user_id = (user_id or "")[:128]
        self.source = (source or "chat")[:32]
        self._runs: dict[str, tuple[float, str, str]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def on_chat_model_start(
        self,
        serialized: dict,
        messages: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self._runs[str(run_id)] = (
            time.monotonic(),
            _model_name(serialized, metadata),
            _messages(messages),
        )

    async def on_llm_start(
        self,
        serialized: dict,
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self._runs[str(run_id)] = (
            time.monotonic(),
            _model_name(serialized, metadata),
            "\n---\n".join(prompts or [])[:MAX_TEXT],
        )

    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        started, model, prompt = self._runs.pop(
            str(run_id), (time.monotonic(), "", "")
        )
        text = ""
        usage: dict[str, Any] = {}
        try:
            generations = getattr(response, "generations", None) or []
            generation = generations[0][0] if generations and generations[0] else None
            message = getattr(generation, "message", None) if generation is not None else None
            if generation is not None:
                text = getattr(generation, "text", "") or ""
                if not text and message is not None:
                    text = getattr(message, "content", "") or ""
            llm_output = getattr(response, "llm_output", None) or {}
            for source in (
                llm_output.get("token_usage"),
                llm_output.get("usage"),
                getattr(generation, "generation_info", None),
                getattr(message, "usage_metadata", None),
                (getattr(message, "response_metadata", None) or {}).get("token_usage"),
            ):
                if hasattr(source, "model_dump"):
                    try:
                        source = source.model_dump()
                    except Exception:  # noqa: BLE001
                        source = None
                if isinstance(source, dict):
                    for key, value in source.items():
                        if value not in (None, 0, ""):
                            usage.setdefault(key, value)
        except (AttributeError, IndexError, TypeError):
            pass
        self._emit(
            {
                "session_id": self.session_id,
                "source": self.source,
                "model": model,
                "prompt": prompt,
                "response": _clip(text),
                "prompt_tokens": _usage_value(usage, "prompt_tokens", "input_tokens"),
                "completion_tokens": _usage_value(
                    usage, "completion_tokens", "output_tokens"
                ),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status": "ok",
                "error": "",
            }
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        started, model, prompt = self._runs.pop(
            str(run_id), (time.monotonic(), "", "")
        )
        self._emit(
            {
                "session_id": self.session_id,
                "source": self.source,
                "model": model,
                "prompt": prompt,
                "response": "",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status": "error",
                "error": _clip(str(error)),
            }
        )

    def _emit(self, payload: dict[str, Any]) -> None:
        try:
            task = asyncio.create_task(self._post(payload))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, payload: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                await client.post(
                    f"{self.base_url}/api/v1/llm/calls",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.key}"},
                )
        except Exception as exc:  # noqa: BLE001 —— 留痕失败不影响对话
            log.debug("skein trace post failed: %s", exc)
