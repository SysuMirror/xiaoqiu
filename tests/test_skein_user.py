import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.deps import verify_request


def sign(payload: dict, secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(secret.encode(), f"v1.{body}".encode(), hashlib.sha256).hexdigest()
    return f"v1.{body}.{sig}"


def request_with(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        }
    )


def header(payload: dict, secret: str = "s3cret") -> str:
    return sign(payload, secret)


def test_skein_user_header_admin(monkeypatch):
    monkeypatch.setenv("SKEIN_USER_SECRET", "s3cret")
    value = header(
        {"sub": "u-1", "internal": True, "admin": True, "exp": int(time.time()) + 60}
    )
    ident = verify_request(request_with({"X-Skein-User": value}))
    assert ident.user_id == "sso:u-1"
    assert ident.role == "admin"


def test_skein_user_header_market(monkeypatch):
    monkeypatch.setenv("SKEIN_USER_SECRET", "s3cret")
    value = header(
        {"sub": "u-2", "internal": True, "admin": False, "exp": int(time.time()) + 60}
    )
    ident = verify_request(request_with({"X-Skein-User": value}))
    assert ident.user_id == "sso:u-2"
    assert ident.role == "user"


def test_skein_user_header_super_admin(monkeypatch):
    monkeypatch.setenv("SKEIN_USER_SECRET", "s3cret")
    value = header(
        {
            "sub": "u-3",
            "internal": True,
            "admin": False,
            "super_admin": True,
            "exp": int(time.time()) + 60,
        }
    )
    ident = verify_request(request_with({"X-Skein-User": value}))
    assert ident.role == "admin"


def test_skein_user_header_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("SKEIN_USER_SECRET", "s3cret")
    value = header({"sub": "u-1", "internal": True, "exp": int(time.time()) + 60}, "other")
    with pytest.raises(HTTPException) as err:
        verify_request(request_with({"X-Skein-User": value}))
    assert err.value.status_code == 401


def test_skein_user_header_rejects_expired(monkeypatch):
    monkeypatch.setenv("SKEIN_USER_SECRET", "s3cret")
    value = header({"sub": "u-1", "internal": True, "exp": int(time.time()) - 1})
    with pytest.raises(HTTPException) as err:
        verify_request(request_with({"X-Skein-User": value}))
    assert err.value.status_code == 401


def test_skein_user_header_ignored_without_secret(monkeypatch):
    # 滚动发布期：上游还没配 secret 时忽略头，仍按 JWT 判定（没有 JWT 就 401）。
    monkeypatch.delenv("SKEIN_USER_SECRET", raising=False)
    value = header({"sub": "u-1", "internal": True, "exp": int(time.time()) + 60})
    with pytest.raises(HTTPException) as err:
        verify_request(request_with({"X-Skein-User": value}))
    assert err.value.status_code == 401
    assert err.value.detail == "missing bearer token"
