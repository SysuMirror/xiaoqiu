from app.frontdesk import (
    _candidate,
    _fallback,
    _open_threads,
    _parse_json,
    _plain,
    _ticket_code,
    _ticket_payload,
    public_message,
)


def test_candidate_follows_the_noise_policy():
    """规则只筛明显无关的内容：带问题词、或点名小秋，才交给模型判断。"""
    assert _candidate("ccomment", "还是不行")
    assert _candidate("post", "登录按钮没反应")
    assert _candidate("post", "@小秋 看下这个")
    assert _candidate("comment", "上传一直失败")
    assert not _candidate("comment", "补充：控制台还报 500")  # 没有规则词也没 @：不打扰
    assert not _candidate("post", "出一台显示器，九成新")
    assert not _candidate("comment", "好")


def test_parse_json_handles_fences():
    assert _parse_json('```json\n{"action":"reply"}\n```') == {"action": "reply"}
    assert _parse_json('前缀 {"action":"skip"} 后缀') == {"action": "skip"}
    assert _parse_json("not json") is None


def test_fallback_bug_and_venting():
    """兜底（无模型时）也遵守同一口径：非 @ 的纯吐槽不回，bug 照常处理。"""
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
    vent_item = {
        "title": "吐槽",
        "text": "这界面也太难用了吧，无语",
        "kind": "post",
        "id": 2,
        "post_id": 2,
        "pcomment_id": 0,
        "user_id": 278,
        "position": "帖子",
    }
    assert _fallback(vent_item)["action"] == "skip"
    vent = _fallback(vent_item, mention=True)
    assert vent["kind"] == "venting"
    assert vent["action"] == "reply"
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


def test_ticket_payload_maps_forum_coordinates():
    """建单请求：source 记形态，external_id 记坐标（幂等键，也是状态回调找回原帖的依据）。"""
    item = {
        "kind": "post",
        "id": 42,
        "post_id": 42,
        "pcomment_id": 0,
        "title": "登录坏了",
        "text": "点登录没反应",
        "user_id": 278,
        "position": "帖子",
    }
    verdict = {"kind": "bug", "title": "登录按钮没反应", "summary": "点登录没反应", "severity": "high"}
    post = _ticket_payload(item, verdict)
    assert (post["source"], post["external_id"]) == ("forum_post", "42")
    assert post["requester"] == "forum:278"
    assert (post["category"], post["severity"]) == ("bug", "high")
    assert post["title"] == "登录按钮没反应"
    assert post["description"] == "点登录没反应"
    assert post["labels"] == ["frontdesk"]

    comment = _ticket_payload({**item, "kind": "comment", "id": 7, "pcomment_id": 7}, verdict)
    assert (comment["source"], comment["external_id"]) == ("forum_comment", "42:7")

    ccomment = _ticket_payload(
        {**item, "kind": "ccomment", "id": 9, "pcomment_id": 7}, verdict
    )
    assert (ccomment["source"], ccomment["external_id"]) == ("forum_ccomment", "42:7:9")


def test_ticket_code_reads_the_api_response():
    assert _ticket_code({"created": True, "ticket": {"code": "SK-1"}}) == "SK-1"
    assert _ticket_code({"created": False, "ticket": {"code": "SK-1"}}) == "SK-1"
    assert _ticket_code(None) == ""
    assert _ticket_code({"detail": {"code": "idempotency_conflict"}}) == ""


def test_open_threads_keeps_only_live_tickets():
    """同一帖已有在途/待验收的单就不再建新单；已结束的不算。"""
    tickets = [
        {"code": "SK-1", "external_id": "42", "status": "review"},
        {"code": "SK-2", "external_id": "42:7", "status": "staged"},
        {"code": "SK-3", "external_id": "43:1", "status": "done"},
        {"code": "SK-4", "external_id": "44", "status": "rejected"},
        {"code": "SK-5", "external_id": "not-a-post", "status": "staged"},
    ]
    assert _open_threads(tickets) == {"42": "SK-1"}
