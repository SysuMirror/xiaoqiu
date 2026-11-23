"""SSE 测试客户端：python tests/sse_client.py "消息" [thread_id]"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")

URL = "http://localhost:8100/task/sse"


def main() -> None:
    message = sys.argv[1]
    thread_id = sys.argv[2] if len(sys.argv) > 2 else "t-default"
    from app.api.deps import mint_token

    body = json.dumps({"message": message, "thread_id": thread_id}).encode("utf-8")
    req = urllib.request.Request(
        URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {mint_token('dev-user', 'admin')}"},
        method="POST",
    )
    full = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:])
                if event == "token":
                    full.append(data["text"])
                    print(data["text"], end="", flush=True)
                elif event == "tool_start":
                    print(f"\n[tool_start] {data['name']} args={data['args']}")
                elif event == "tool_end":
                    print(f"[tool_end] {data['name']}: {data['preview'][:100]}")
                elif event == "error":
                    print(f"\n[ERROR] {data}")
                elif event == "done":
                    print(f"\n[done] {data}")
    if full:
        print()


if __name__ == "__main__":
    main()
