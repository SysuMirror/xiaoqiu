"""校验雁阵状态回调的签名。

雁阵给每个助手配一把回调签名密钥（``whsec_…``），发回调时带两个头：

  X-Skein-Webhook-Timestamp: <unix 秒>
  X-Skein-Webhook-Signature: v1=<hex(HMAC-SHA256(secret, "<timestamp>.<原始请求体>"))>

签名串里带时间戳，旧回调原样重放会因超出容差被拒。比较用恒定时间函数。
"""

from __future__ import annotations

import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def signature(body: bytes, timestamp: str, secret: str) -> str:
    """按雁阵的约定算签名（测试与排障用）。"""
    message = f"{timestamp}.".encode() + body
    return "v1=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    body: bytes,
    timestamp: str | None,
    signature_header: str | None,
    secret: str,
    now: float | None = None,
) -> bool:
    """请求体、时间戳、签名头、密钥 → 是否可信。缺任何一样或过期都返回 False。"""
    if not secret or not timestamp or not signature_header:
        return False
    if not timestamp.isdigit():
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(signature(body, timestamp, secret), signature_header)
