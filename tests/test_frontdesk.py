from app import frontdesk
from app.frontdesk import (
    _candidate,
    _fallback,
    _parse_json,
    _plain,
    _ticket_payload,
    public_message,
)


def test_candidate_keeps_comment_followups():
    assert _candidate("comment", "补充：控制台还报 500")
    assert _candidate("ccomment", "还是不行")
    assert _candidate("post", "登录按钮没反应")
    assert _candidate("post", "@小秋 看下这个")
    assert not _candidate("post", "出一台显示器，九成新")
    assert not _candidate("comment", "好")


def test_parse_json_handles_fences():
    assert _parse_json('```json\n{"action":"reply"}\n```') == {"action": "reply"}
    assert _parse_json('前缀 {"action":"skip"} 后缀') == {"action": "skip"}
    assert _parse_json("not json") is None


def test_fallback_bug_and_venting():
    bug = _fallback(
        {
            "title": "登录坏了",
            "text": "点登录按钮没反应，报错了",
            "kind": "post",
            "id": 1,
            "post_id": 1,
            "pcomment_id": 0,
            "user_id": 278,
            "position": "帖子",
        }
    )
    assert bug["action"] == "reply"
    assert bug["kind"] == "bug"
    vent = _fallback(
        {
            "title": "吐槽",
            "text": "这界面也太难用了吧，无语",
            "kind": "post",
            "id": 2,
            "post_id": 2,
            "pcomment_id": 0,
            "user_id": 278,
            "position": "帖子",
        }
    )
    assert vent["kind"] == "venting"
    skip = _fallback(
        {
            "title": "广告",
            "text": "出一台显示器，九成新",
            "kind": "post",
            "id": 3,
            "post_id": 3,
            "pcomment_id": 0,
            "user_id": 278,
            "position": "帖子",
        }
    )
    assert skip["action"] == "skip"


def test_plain_and_public_message():
    assert _plain("<p>hello&nbsp; world</p>") == "hello&nbsp; world"
    assert "已修复" in public_message("fixed", "SK-1")
    assert "SK-1" in public_message("working", "SK-1")
    assert public_message("unknown", "SK-1") == ""


# ---------------- Skein 建单：助手 key + 通用字段 ----------------


def test_ticket_payload_maps_forum_items_to_generic_fields():
    item = {
        "kind": "ccomment",
        "id": 9,
        "post_id": 42,
        "pcomment_id": 7,
        "title": "帖子标题",
        "text": "还是报错",
        "user_id": 278,
    }
    verdict = {"title": "楼中楼补充", "summary": "还是报错", "severity": "urgent"}
    payload = _ticket_payload(item, verdict, "bug")
    # source / external_id 等于旧的 origin / origin_id：Skein 回帖靠它们反查帖子与楼层
    assert payload["source"] == "forum_ccomment"
    assert payload["external_id"] == "42:7:9"
    assert payload["thread_id"] == "42"
    assert payload["requester"] == {"id": "forum:278"}
    assert (payload["category"], payload["severity"]) == ("bug", "normal")  # 不认的级别回落
    assert payload["url"].endswith("/postdetail/42")
    assert "visibility" not in payload and "message_id" not in payload
    assert _ticket_payload({**item, "kind": "comment", "id": 7}, verdict, "bug")["external_id"] == "42:7"
    assert _ticket_payload({**item, "kind": "post", "id": 42}, verdict, "bug")["external_id"] == "42"
    assert _ticket_payload(item, {**verdict, "severity": "high"}, "bug")["severity"] == "high"


async def test_submit_uses_the_assistant_key_and_never_the_shared_token(monkeypatch):
    calls = []

    async def fake_post(path, payload, headers, timeout):
        calls.append((path, headers))
        return outcome[0]

    monkeypatch.setattr(frontdesk, "_skein_post", fake_post)
    monkeypatch.setenv("SKEIN_TOKEN", "shared-token")
    monkeypatch.setenv("SKEIN_ASSISTANT_KEY", "skein_asst_x")

    outcome = [(201, {"code": "SK-1", "created": True})]
    assert await frontdesk._skein_submit({"title": "t"}) == {"code": "SK-1", "created": True}
    assert calls == [("/api/v1/tickets", {"Authorization": "Bearer skein_asst_x"})]

    # 409：同一条内容文本变了再报，仍然拿回原工单
    outcome = [
        (409, {"detail": {"code": "duplicate_external_id", "existing": {"id": "a", "code": "SK-2"}}})
    ]
    assert (await frontdesk._skein_submit({"title": "t"}))["code"] == "SK-2"

    # 其他错误、没配 key：拿不到工单
    outcome = [(401, {"detail": {"code": "invalid_key"}})]
    assert await frontdesk._skein_submit({"title": "t"}) is None
    monkeypatch.delenv("SKEIN_ASSISTANT_KEY")
    assert await frontdesk._skein_submit({"title": "t"}) is None


async def test_trace_still_goes_through_the_shared_token(monkeypatch):
    calls = []

    async def fake_post(path, payload, headers, timeout):
        calls.append((path, headers))
        return 200, {"ok": True}

    monkeypatch.setattr(frontdesk, "_skein_post", fake_post)
    monkeypatch.setenv("SKEIN_TOKEN", "shared-token")
    monkeypatch.delenv("SKEIN_ASSISTANT_KEY", raising=False)
    assert await frontdesk._skein_call("/internal/llm/calls", {}) == {"ok": True}
    assert calls == [("/internal/llm/calls", {"X-Skein-Token": "shared-token"})]


class FakePool:
    def __init__(self) -> None:
        self.marks = []

    async def fetchval(self, query, *args):
        return 0 if "count(*)" in query else None  # 没处理过，近一小时也没回过

    async def execute(self, query, kind, item_id, status):
        self.marks.append((kind, item_id, status))


async def test_handle_quotes_the_code_only_when_skein_returns_one(monkeypatch):
    posted, replies = [], []
    outcome = [(201, {"code": "SK-1", "created": True})]

    async def fake_post(path, payload, headers, timeout):
        posted.append(headers)
        return outcome[0]

    async def fake_reply(post_id, pcomment_id, content, reply_ccomment_id=0):
        replies.append(content)
        return True

    monkeypatch.setattr(frontdesk, "_skein_post", fake_post)
    monkeypatch.setattr(frontdesk, "post_reply", fake_reply)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # classify 走 _fallback，不联网
    monkeypatch.delenv("XQ_BOT_UID", raising=False)
    monkeypatch.setenv("SKEIN_ASSISTANT_KEY", "skein_asst_x")
    monkeypatch.setenv("SKEIN_TOKEN", "shared-token")
    item = {
        "kind": "post",
        "id": 42,
        "post_id": 42,
        "pcomment_id": 0,
        "title": "登录坏了",
        "text": "点登录按钮没反应，报错了",
        "user_id": 278,
        "position": "帖子",
    }
    base_reply = "收到，已登记，我先复现一下；有复现步骤或截图可以回我。"

    pool = FakePool()
    await frontdesk.handle(pool, item)
    assert replies == [base_reply + "（SK-1）"]
    assert pool.marks == [("post", 42, "replied")]

    # Skein 没给出工单（比如 key 失效）：照常回复，但不追加编号
    outcome = [(401, {"detail": {"code": "invalid_key"}})]
    await frontdesk.handle(pool, {**item, "id": 43, "post_id": 43})
    assert replies == [base_reply + "（SK-1）", base_reply]
    assert pool.marks[-1] == ("post", 43, "replied")

    # 建单只带助手 key，从不带共享令牌
    assert posted == [{"Authorization": "Bearer skein_asst_x"}] * 2
