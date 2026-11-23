"""邮件通道：MailPort 抽象 + Jinja2 模板（autoescape）+ 收件人白名单 + 去重窗口。

dev 用 LogMail（落盘+台账可验收）；拿到 SMTP 授权码后配置 SmtpMail 即切换。
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = ROOT / "app" / "tools" / "notify" / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),  # ADR：邮件强制转义
)

RECIPIENT_WHITELIST: dict[str, str] = {}  # 组别名 -> 真实地址，由 init 注入
_dedup: dict[str, float] = {}  # dedup_key -> last sent ts
DEDUP_WINDOW = 600  # 10 分钟同类合并


class MailCfg(BaseModel):
    mode: str = "log"  # log | smtp
    smtp_host: str = ""
    smtp_port: int = 465
    user_env: str = "SMTP_USER"
    pass_env: str = "SMTP_PASS"
    from_name: str = "小秋 · SSE Market 助手"
    recipients: dict[str, str] = {}  # 组别名 -> 地址


def init_mail(cfg: MailCfg) -> None:
    RECIPIENT_WHITELIST.clear()
    RECIPIENT_WHITELIST.update(cfg.recipients)


def render(template: str, **ctx) -> str:
    return _env.get_template(f"{template}.html").render(**ctx)


async def send(group: str, subject: str, html: str, dedup_key: str | None = None) -> str:
    """发邮件到组别名（白名单）。返回 sent / deduped / rejected。"""
    import os

    to_addr = RECIPIENT_WHITELIST.get(group)
    if not to_addr:
        log.warning("mail rejected: group %s not in whitelist", group)
        return "rejected"
    if dedup_key:
        last = _dedup.get(dedup_key)
        # 注意默认 None 而非 0：monotonic() 是开机秒数，刚启动的机器 uptime < 窗口时会误判为"刚发过"
        if last is not None and time.monotonic() - last < DEDUP_WINDOW:
            log.info("mail deduped: %s", dedup_key)
            return "deduped"
        _dedup[dedup_key] = time.monotonic()

    cfg: MailCfg = get_mail_cfg()
    if cfg.mode == "smtp":
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((cfg.from_name, os.environ[cfg.user_env]))
        msg["To"] = to_addr
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.login(os.environ[cfg.user_env], os.environ[cfg.pass_env])
            s.sendmail(os.environ[cfg.user_env], [to_addr], msg.as_string())
    else:  # log 模式：落盘供验收
        out = ROOT / "var" / "mail_outbox"
        out.mkdir(parents=True, exist_ok=True)
        fname = out / f"{int(time.time() * 1000)}_{group}.html"
        fname.write_text(
            json.dumps({"to": to_addr, "subject": subject}, ensure_ascii=False) + "\n" + html,
            encoding="utf-8",
        )
        log.info("mail (log mode) written: %s subject=%s", fname.name, subject)
    return "sent"


_mail_cfg: MailCfg | None = None


def get_mail_cfg() -> MailCfg:
    global _mail_cfg
    assert _mail_cfg is not None, "mail not initialized"
    return _mail_cfg


def setup_mail(cfg: MailCfg) -> None:
    global _mail_cfg
    _mail_cfg = cfg
    init_mail(cfg)
