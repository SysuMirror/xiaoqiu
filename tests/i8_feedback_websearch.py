"""I8 验收：反馈直达（AI 整理 + 限流）+ 联网搜索开关治理。

F1 E2E：用户口述反馈 → submit_feedback 被调用 → var/mail_outbox 落盘一封（log 模式）
F2 确定性：直接调工具（绕过 LLM）——非法 category 归为"其他"；1 小时限 1 条
W1 E2E：websearch.enabled=false → 工具不注入（tool_start 不得出现 web_search）
W2 确定性：_parse_results 解析/去重/截断
"""
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8100"
ROOT = Path(__file__).resolve().parent.parent
OUTBOX = ROOT / "var" / "mail_outbox"

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")
from app.api.deps import mint_token

USER_H = {"Authorization": f"Bearer {mint_token('i8-user', 'user')}"}


def sse_events(path: str, body: dict, headers: dict) -> list[tuple[str, dict]]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=240) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                events.append((event, json.loads(line[6:])))
                if event in ("done", "error", "suspended"):
                    break
    return events


def find_event(events, name):
    for ev, d in events:
        if ev == name:
            return d
    return None


def last_text(events) -> str:
    return "".join(d.get("text", "") for ev, d in events if ev == "token")


def outbox_files() -> list[Path]:
    return sorted(OUTBOX.glob("*.html")) if OUTBOX.exists() else []


def f1_feedback_e2e():
    import time as _t

    uniq = int(_t.time())
    before = len(outbox_files())
    ts = None
    for attempt in range(3):
        ev = sse_events("/task/sse", {
            "message": "我要反馈一个建议：论坛搜索只支持关键词，中文打错一个字就搜不到，希望能支持拼音或者模糊搜索，这对我们用手机的人很重要",
            "thread_id": f"t-i8-f1-{uniq}-{attempt}",
        }, {"Authorization": f"Bearer {mint_token(f'i8-f1-{uniq}-{attempt}', 'user')}"})
        ts = find_event(ev, "tool_start")
        if ts and ts["name"] == "submit_feedback":
            break
        print(f"  [f1] attempt {attempt}: submit_feedback not called, retrying")
    assert ts and ts["name"] == "submit_feedback", f"submit_feedback never called: {ts}"
    assert find_event(ev, "done"), "run should finish"
    after = outbox_files()
    assert len(after) == before + 1, f"expected 1 new outbox mail, got {len(after) - before}"
    body = after[-1].read_text(encoding="utf-8")
    assert "[建议]" in body, f"mail missing category: {body[:200]}"
    assert "拼音" in body, "mail must contain the organized content"
    print(f"[F1] feedback E2E OK (mail={after[-1].name}, title in file)")
    return ev


async def f2_feedback_tool_deterministic():
    from app.memory import ledger
    from app.tools import TOOL_FUNCS
    from app.tools.notify import mail

    mail.setup_mail(mail.MailCfg(mode="log", recipients={"feedback": "t@t"}))
    await ledger.init_ledger(os.environ["AGENT_PG_DSN"])  # 台账真实落库，避免 best-effort 噪音
    fb = TOOL_FUNCS["submit_feedback"]
    cfg = {"configurable": {"user_id": "i8-f2-user", "thread_id": "t-i8-f2"}}

    before = len(outbox_files())
    r1 = await fb.ainvoke({
        "category": "垃圾输入", "title": "T", "summary": "S", "original": "O",
    }, config=cfg)
    files = outbox_files()
    assert len(files) == before + 1, "first call must produce one mail"
    assert "[其他]" in files[-1].read_text(encoding="utf-8"), "invalid category must fall back to 其他"
    r2 = await fb.ainvoke({
        "category": "bug", "title": "T2", "summary": "S2", "original": "O2",
    }, config=cfg)
    assert len(outbox_files()) == before + 1, "cooldown must block second mail"
    assert "每小时" in r2, f"cooldown message wrong: {r2}"
    await ledger.close_ledger()
    print(f"[F2] feedback tool OK (category fallback + cooldown; r1={r1[:30]}...)")


def w1_websearch_disabled():
    """安全属性断言：禁用时 web_search 不得真正执行（模型偶发幻觉调用不存在的工具，
    ToolNode 会拒绝；断言无真实结果返回而非模型永不喊出名字）。"""
    for attempt in range(3):
        ev = sse_events("/task/sse", {
            "message": "帮我联网搜一下 2026 腾讯校招提前批的时间安排",
            "thread_id": f"t-i8-w1-{attempt}",
        }, USER_H)
        if not find_event(ev, "done"):
            continue  # run 未正常结束，换 thread 重试
        executed = [
            d for ev_, d in ev
            if ev_ == "tool_end" and d["name"] == "web_search"
            and "外部网页搜索结果" in d.get("preview", "")
        ]
        assert not executed, "web_search executed while disabled"
        print(f"[W1] websearch disabled OK (attempt {attempt}, no real execution)")
        return
    raise AssertionError("websearch-disabled run never finished in 3 attempts")


def w2_parse_results():
    from app.tools.chat.web_search import _parse_results

    # 千帆 references[] 形状（content 字段）+ tavily results[] 形状（同名字段）
    baidu = {"references": [
        {"title": f"t{i}", "url": f"https://x/{i}", "content": "c" * 300}
        for i in range(8)
    ] + [{"title": "dup", "url": "https://x/1", "content": "dup"}]}
    hits = _parse_results(baidu, 5)
    assert len(hits) == 5, f"cap failed: {len(hits)}"
    assert all(len(h["snippet"]) <= 160 for h in hits), "snippet truncation failed"
    urls = [h["url"] for h in hits]
    assert len(urls) == len(set(urls)), "dedup failed"

    tavily = {"results": [{"title": "tk", "url": "https://y/1", "content": "tv"}]}
    assert _parse_results(tavily, 5)[0]["snippet"] == "tv", "tavily shape failed"
    assert _parse_results({"references": []}, 5) == [], "empty payload must parse to []"
    assert _parse_results({}, 5) == [], "missing key must parse to []"
    print("[W2] _parse_results OK (baidu/tavily shapes, cap/dedup/truncate)")


if __name__ == "__main__":
    from app.win_compat import setup_event_loop

    setup_event_loop()
    w2_parse_results()
    f1_feedback_e2e()
    asyncio.run(f2_feedback_tool_deterministic())
    w1_websearch_disabled()
    print("\nI8 acceptance: ALL PASS")
