"""SMTP 发送：基于 email-ops skill 的 send_email.py 扩展。

支持多收件人/抄送、签名拼接、Message-ID 返回（用于线程追踪）。
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional


@dataclass
class SmtpConfig:
    host: str
    port: int
    use_ssl: bool          # True = 465 直连 SSL；False = STARTTLS
    username: str
    password: str


def build_message(
    from_display: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    cc_addrs: Optional[list[str]] = None,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_display, from_addr)) if from_display else from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    # Message-ID 用于回复追踪与线程聚合；Date 为标准必需头
    domain = from_addr.split("@")[-1] if "@" in from_addr else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


def send_message(cfg: SmtpConfig, msg: MIMEMultipart, envelope_from: str) -> str:
    """发送并返回 Message-ID。失败时抛出 SmtpSendError。"""
    recipients = [a for a in (msg["To"], msg["Cc"]) if a]
    all_addrs = []
    for part in recipients:
        all_addrs.extend([x.strip() for x in part.split(",") if x.strip()])

    try:
        if cfg.use_ssl or cfg.port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=30) as server:
                server.login(cfg.username, cfg.password)
                server.sendmail(envelope_from, all_addrs, msg.as_string())
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(cfg.username, cfg.password)
                server.sendmail(envelope_from, all_addrs, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        raise SmtpSendError(f"认证失败（检查用户名/密码或应用专用密码）: {detail}") from e
    except smtplib.SMTPRecipientsRefused as e:
        raise SmtpSendError(f"收件人被拒绝: {e}") from e
    except smtplib.SMTPDataError as e:
        detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        raise SmtpSendError(f"服务器拒收（可能被判为垃圾邮件）: {detail}") from e
    except smtplib.SMTPConnectError as e:
        raise SmtpSendError(f"无法连接 {cfg.host}:{cfg.port}") from e
    except TimeoutError as e:
        raise SmtpSendError(f"连接超时 {cfg.host}:{cfg.port}") from e
    except OSError as e:
        raise SmtpSendError(f"网络错误: {e}") from e

    return msg["Message-ID"] or ""


def test_connection(cfg: SmtpConfig) -> None:
    """只验证能否登录，不发送。失败抛 SmtpSendError。"""
    try:
        if cfg.use_ssl or cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ssl.create_default_context(), timeout=20) as server:
                server.login(cfg.username, cfg.password)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(cfg.username, cfg.password)
    except smtplib.SMTPAuthenticationError as e:
        detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        raise SmtpSendError(f"SMTP 认证失败: {detail}") from e
    except (smtplib.SMTPConnectError, TimeoutError, OSError) as e:
        raise SmtpSendError(f"无法连接 {cfg.host}:{cfg.port} — {e}") from e


class SmtpSendError(Exception):
    pass
