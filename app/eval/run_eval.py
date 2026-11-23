"""评测流水线：golden cases 回归。手动跑（ADR-不进 CI）。

用法: python -m app.eval.run_eval [--server http://localhost:8100]
输出: eval/reports/<ts>.json，含每用例通过性与耗时，看趋势用。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "app" / "eval" / "golden_cases.yaml"
REPORTS = ROOT / "eval" / "reports"


def load_token(role: str) -> str:
    import os

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    os.environ.setdefault("JWT_SECRET", "dev-only-secret")
    from app.api.deps import mint_token

    return mint_token(f"eval-{role}", role)


def run_case(server: str, case: dict, token: str) -> dict:
    body = json.dumps({"message": case["question"], "thread_id": f"eval-{case['id']}-{int(time.time())}"}).encode()
    req = urllib.request.Request(
        f"{server}/task/sse", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    tools_called, reply = [], ""
    with urllib.request.urlopen(req, timeout=300) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:])
                if event == "tool_start":
                    tools_called.append(data["name"])
                elif event == "token":
                    reply += data["text"]
                elif event in ("done", "error", "suspended"):
                    break
    checks = {
        "tools_hit": not case.get("expect_tools") or any(t in tools_called for t in case["expect_tools"]),
        "contains": not case.get("expect_contains") or any(k in reply for k in case["expect_contains"]),
        "forbid": not any(k in reply for k in case.get("expect_forbid", [])),
    }
    return {
        "id": case["id"],
        "tools_called": tools_called,
        "reply_head": reply[:80],
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8100")
    ap.add_argument("--retry", type=int, default=2, help="LLM 非确定性重试次数")
    args = ap.parse_args()

    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    tokens = {"user": load_token("user"), "admin": load_token("admin")}

    results = []
    for case in cases:
        passed, last = False, None
        for _ in range(max(1, args.retry)):
            t0 = time.time()
            last = run_case(args.server, case, tokens[case["role"]])
            last["elapsed_s"] = round(time.time() - t0, 1)
            if last["passed"]:
                passed = True
                break
        results.append(last if last else {"id": case["id"], "passed": False})
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {case['id']} tools={last['tools_called']} {last.get('elapsed_s','?')}s")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "results": results,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{report['passed']}/{report['total']} passed -> {out}")


if __name__ == "__main__":
    main()
