"""鉴权依赖：JWT(HS256) / Skein 身份头 → UserIdentity。身份只来自 token，永不来自 LLM 或请求体。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt
from fastapi import HTTPException, Request

if TYPE_CHECKING:  # 只用于类型；别把整个工具注册表拖进鉴权模块
    from app.tools.registry import Role


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    role: Role


def _secret() -> str:
    return os.environ.get("JWT_SECRET", "")


def mint_token(user_id: str, role: Role, expires_hours: int = 8) -> str:
    """dev/测试用签发器。生产由 unisso/主站签发，同构 payload。
    iat 回拨 5s：客户端时钟略快于服务器时避免 PyJWT ImmatureSignatureError。"""
    import time

    payload = {
        "sub": user_id,
        "role": role,
        "iat": int(time.time()) - 5,
        "exp": int(time.time()) + expires_hours * 3600,
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def _skein_user_secret() -> str:
    return os.environ.get("SKEIN_USER_SECRET", "")


def _verify_skein_user(request: Request) -> UserIdentity | None:
    """Skein 代理带的身份头：v1.<base64url>.<hmac>；未配置 secret 时忽略，回退 JWT。"""
    raw = request.headers.get("X-Skein-User", "")
    if not raw:
        return None
    secret = _skein_user_secret()
    if not secret:
        return None
    try:
        version, body, sig = raw.split(".")
        if version != "v1":
            raise ValueError
        expected = hmac.new(
            secret.encode(), f"v1.{body}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        padding = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + padding))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid user header")
    if not isinstance(data, dict) or not data.get("sub"):
        raise HTTPException(status_code=401, detail="invalid user header")
    try:
        if int(data.get("exp", 0)) < int(time.time()):
            raise HTTPException(status_code=401, detail="user header expired")
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="invalid user header")
    # unisso is_admin 才是管理员；internal 只代表集市用户。
    role: Role = "admin" if (data.get("admin") or data.get("super_admin")) else "user"
    return UserIdentity(user_id=f"sso:{data['sub']}", role=role)


def verify_request(request: Request) -> UserIdentity:
    ident = _verify_skein_user(request)
    if ident is not None:
        return ident
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth[7:]
    try:
        # leeway=5s：容忍客户端/签发方时钟的小幅偏移（iat 未来几秒不算非法）
        payload = jwt.decode(token, _secret(), algorithms=["HS256"], leeway=5)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")

    # 兼容两种签发方：
    # 1) 本服务 dev mint: {sub, role}
    # 2) 主站 Go: {UserID: int, sub: "user token"|"admin token"(RegisteredClaims.Subject)}
    user_id = str(payload.get("sub", "") or payload.get("UserID") or "").strip()
    role = payload.get("role")
    if not role:
        subject = str(payload.get("sub") or payload.get("Subject") or "")
        role = "admin" if "admin" in subject.lower() else "user"
        if payload.get("UserID") is not None:
            user_id = f"u{payload['UserID']}"
        elif payload.get("AdminID") is not None:
            user_id = f"a{payload['AdminID']}"
    if not user_id or role not in ("user", "admin"):
        raise HTTPException(status_code=401, detail="token missing sub/role")
    if role == "admin":
        # v3 加固：admin 白名单（settings admin.uid_allowlist 非空时强制）；
        # 允许 a 前缀（AdminID）与白名单内 u 前缀（主站管理员同体账号）。
        allow = _admin_allowlist()
        raw = user_id[1:] if user_id[:1] in ("a", "u") else user_id
        if allow and (not raw.isdigit() or int(raw) not in allow):
            import logging as _lg

            _lg.getLogger("sse-agent").warning(
                "admin token rejected by allowlist (user_id=%s)", user_id)
            raise HTTPException(status_code=403, detail="admin not allowed")
    return UserIdentity(user_id=user_id, role=role)


def _admin_allowlist() -> set[int]:
    try:
        from app.config import get_config

        return {int(x) for x in get_config().admin.uid_allowlist}
    except Exception:  # noqa: BLE001 —— 配置缺失时保持旧行为
        return set()
