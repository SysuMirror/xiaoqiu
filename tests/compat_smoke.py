"""兼容层冒烟测试：按旧小秋契约走一遍 bootstrap → task/sse → content 事件。"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")
from app.api.deps import mint_token

H = {"Authorization": f"Bearer {mint_token('compat-user', 'user')}",
     "Content-Type": "application/json"}
BASE = "http://localhost:8100"


def main():
    # bootstrap
    req = urllib.request.Request(BASE + "/api/v1/agent/conversation/bootstrap",
                                 data=b"{}", headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    assert d["code"] == 0 and d["data"]["session_id"], d
    sid = d["data"]["session_id"]
    print(f"[bootstrap] OK session_id={sid[:12]}...")

    # task/sse
    body = json.dumps({"task": "论坛最新的2条帖子是什么？", "session_id": sid}).encode()
    req = urllib.request.Request(BASE + "/api/v1/agent/task/sse", data=body, headers=H, method="POST")
    modes, text = set(), ""
    with urllib.request.urlopen(req, timeout=240) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:])
                if event == "content":
                    modes.add(data["mode"])
                    if data["mode"] in ("replace", "final"):
                        text = data["text"]
                    else:
                        text += data["text"]
                elif event == "error":
                    raise AssertionError(f"error event: {data}")
    assert "final" in modes or "append" in modes, modes
    assert "postdetail" in text or "帖子" in text, f"reply: {text[:120]}"
    print(f"[task/sse] OK modes={sorted(modes)} reply[:60]={text[:60]}")
    print("\nCompat smoke: PASS")


if __name__ == "__main__":
    main()
