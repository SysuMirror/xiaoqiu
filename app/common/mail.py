"""统一邮件发送：经 Go /internal/mail，凭据只在 Go 侧。"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from app.common.goaddr import resolve_go_base
from app.config import get_config
from app.observability import metrics

log = logging.getLogger(__name__)


async def send_mail(subject: str, body: str) -> str:
    cfg = get_config().ops_guard
    if not cfg.mail_enabled:
        metrics.inc("guard_mail_total", {"status": "disabled"})
        return "disabled"
    token = os.environ.get(cfg.mail_token_env, "")
    status = "failed"
    try:
        mail_url = cfg.mail_url
        dyn = await resolve_go_base()
        if dyn:
            mail_url = f"{dyn}{urlparse(cfg.mail_url).path}"
        async with httpx.AsyncClient(timeout=8, trust_env=False) as hc:
            r = await hc.post(
                mail_url,
                headers={"X-Internal-Token": token},
                json={"to": cfg.email_to, "subject": subject[:200], "body": body[:16000]},
            )
            status = "sent" if r.status_code == 200 else f"http{r.status_code}"
    except Exception as e:  # noqa: BLE001 —— 邮件失败只落日志和计数
        log.warning("mail failed: %s: %s", type(e).__name__, e)
    metrics.inc("guard_mail_total", {"status": status.split("http")[0]})
    return status
