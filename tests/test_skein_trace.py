import asyncio
from typing import ClassVar

from app.observability.skein_trace import SkeinTraceCallback


class FakeMessage:
    type = "human"
    content = "帮我看看登录"


class FakeGeneration:
    text = "好的，先复现一下"
    generation_info: ClassVar[dict] = {"prompt_tokens": 3, "completion_tokens": 5}


class FakeResponse:
    generations: ClassVar[list] = [[FakeGeneration()]]
    llm_output: ClassVar[dict] = {}


def make_callback(captured):
    callback = SkeinTraceCallback(
        "http://skein.test", "skein_at_key", session_id="s1", user_id="u1", source="chat"
    )
    callback._emit = captured.append
    return callback


def test_chat_model_call_is_reported():
    captured = []
    callback = make_callback(captured)
    asyncio.run(
        callback.on_chat_model_start(
            {"kwargs": {"model": "qwen-test"}},
            [[FakeMessage()]],
            run_id="r1",
        )
    )
    asyncio.run(callback.on_llm_end(FakeResponse(), run_id="r1"))
    assert len(captured) == 1
    payload = captured[0]
    assert payload["session_id"] == "s1"
    assert payload["source"] == "chat"
    # 调用归属由雁阵按凭证认定：报文里不再带 actor
    assert "actor" not in payload
    assert payload["model"] == "qwen-test"
    assert "帮我看看登录" in payload["prompt"]
    assert payload["response"] == "好的，先复现一下"
    assert payload["prompt_tokens"] == 3
    assert payload["completion_tokens"] == 5
    assert payload["status"] == "ok"


def test_llm_error_is_reported():
    captured = []
    callback = make_callback(captured)
    asyncio.run(
        callback.on_llm_start(
            {"kwargs": {"model": "m"}}, ["prompt"], run_id="r2"
        )
    )
    asyncio.run(callback.on_llm_error(RuntimeError("boom"), run_id="r2"))
    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    assert "boom" in captured[0]["error"]
