"""I2 验收：JWT 鉴权 + thread 属主 + RBAC 动态注入。

U1: 无 token → 401
U2: 伪造 token → 401
U3: 过期 token → 401
U4: user 角色图里没有 kill_process（结构级断言，确定性）
U5: user E2E 要求杀容器 → 不产生审批/执行，容器存活
U6: user A 创建 thread，user B 访问 → 403
U7: admin 正常对话可用
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8100"
TEST_CONTAINER = "sse_test_sleep"

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("JWT_SECRET", "dev-only-secret")
import jwt

from app.api.deps import mint_token

ADMIN_H = {"Authorization": f"Bearer {mint_token('i2-admin', 'admin')}"}
USER_A_H = {"Authorization": f"Bearer {mint_token('i2-user-a', 'user')}"}
USER_B_H = {"Authorization": f"Bearer {mint_token('i2-user-b', 'user')}"}


def http_status(path: str, body: dict, headers: dict) -> int:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def ssh_docker(*args: str) -> str:
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "SSE_Market", "docker", *args],
        capture_output=True, text=True, timeout=60,
    )
    return (out.stdout + out.stderr).strip()


def container_running(name: str) -> bool:
    return bool(ssh_docker("ps", "--format", "{{.Names}}", "--filter", f"name=^{name}$"))


def u1_u2_u3():
    body = {"message": "hi", "thread_id": "t-i2-auth"}
    assert http_status("/task/sse", body, {}) == 401, "no token must be 401"
    forged = {"Authorization": "Bearer " + jwt.encode(
        {"sub": "hacker", "role": "admin", "exp": int(time.time()) + 3600}, "wrong-secret", algorithm="HS256")}
    assert http_status("/task/sse", body, forged) == 401, "forged token must be 401"
    expired = {"Authorization": "Bearer " + mint_token("x", "admin", expires_hours=-1)}
    assert http_status("/task/sse", body, expired) == 401, "expired token must be 401"
    print("[U1-U3] auth 401 cases OK")


def u4_structural():
    from app.tools.registry import tools_for_role

    user_tools = tools_for_role("user")
    assert "kill_process" not in user_tools, f"user tools must not include kill_process: {user_tools}"
    assert "sql_post_search" in user_tools
    print(f"[U4] user toolset OK (kill_process absent): {user_tools}")


def u5_e2e_user_no_kill():
    ssh_docker("rm", "-f", TEST_CONTAINER)
    ssh_docker("run", "-d", "--name", TEST_CONTAINER, "busybox", "sleep", "3600")
    req = urllib.request.Request(
        BASE + "/task/sse",
        data=json.dumps({
            "message": f"必须使用 kill_process 工具停止容器 {TEST_CONTAINER}，原因是测试",
            "thread_id": "t-i2-u5",
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", **USER_A_H},
        method="POST",
    )
    got_approval, done = False, False
    with urllib.request.urlopen(req, timeout=180) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                if event == "approval_request":
                    got_approval = True
                if event in ("done", "error"):
                    done = True
                    break
    assert done and not got_approval, f"user must not reach approval: approval={got_approval} done={done}"
    assert container_running(TEST_CONTAINER), "container must survive user request"
    print("[U5] user E2E: no approval path, container alive OK")


def u6_ownership():
    # A 创建 thread
    st = http_status("/task/sse", {"message": "你好", "thread_id": "t-i2-shared"}, USER_A_H)
    assert st == 200, f"A create thread failed: {st}"
    # B 访问同一 thread → 403（注意：SSE 已开始流式，403 在建连前抛出）
    st = http_status("/task/sse", {"message": "你好", "thread_id": "t-i2-shared"}, USER_B_H)
    assert st == 403, f"B access to A's thread must be 403, got {st}"
    print("[U6] thread ownership OK")


def u7_admin_ok():
    st = http_status("/task/sse", {"message": "你好", "thread_id": "t-i2-admin"}, ADMIN_H)
    assert st == 200, f"admin chat failed: {st}"
    print("[U7] admin chat OK")


if __name__ == "__main__":
    u1_u2_u3()
    u4_structural()
    u5_e2e_user_no_kill()
    u6_ownership()
    u7_admin_ok()
    print("\nI2 acceptance: ALL PASS")
