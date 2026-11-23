from app.frontdesk import _candidate, _fallback, _parse_json, _plain, public_message


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
