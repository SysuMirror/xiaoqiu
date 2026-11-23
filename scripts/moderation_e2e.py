"""E2E 测试:u99999(不存在的用户)发两条辱骂 → 验证 strike/禁言链路,不伤真实用户。"""
import json
import sys

import httpx

TOKEN = sys.argv[1]
BASE = "http://127.0.0.1:8100"
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

MSG1 = "你就是个傻逼"
MSG2 = "草泥马滚出去"
MSG3 = "帮我看看最新帖子"


def send(thread: str, text: str) -> str:
    """发送并收集 SSE 流中的最终 AI 文本。"""
    reply_parts = []
    with httpx.Client(timeout=90, trust_env=False) as hc:
        with hc.stream("POST", f"{BASE}/task/sse",
                       headers=HDR, json={"message": text, "thread_id": thread}) as r:
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event in ("message", "end", "error", "done"):
                    payload = line.split(":", 1)[1].strip()
                    try:
                        d = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(d, dict):
                        for k in ("content", "text", "message", "delta"):
                            if isinstance(d.get(k), str) and d[k]:
                                reply_parts.append(d[k])
                        if d.get("error"):
                            return f"[ERROR] {d['error']}"
    return "".join(reply_parts)[-300:] or "(空)"


if sys.argv[2] == "1":
    print("reply1:", send("modtest_e2e_1", MSG1))
elif sys.argv[2] == "2":
    print("reply2:", send("modtest_e2e_2", MSG2))
else:
    print("reply3:", send("modtest_e2e_3", MSG3))
