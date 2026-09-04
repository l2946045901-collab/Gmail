"""邮件解析：header decode、正文提取、退信识别、变量渲染。"""

import email
import email.utils
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from typing import Optional

from .. import config

_MIME_VERSION_RE = re.compile(r"multipart/report", re.IGNORECASE)


def decode_header_value(raw: str) -> str:
    """解码可能含编码词的 header（如 =?UTF-8?B?...?=）。"""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def parse_addr_list(raw: str) -> list[tuple[str, str]]:
    """解析地址列表，返回 [(display_name, addr), ...]。"""
    if not raw:
        return []
    return [(decode_header_value(name), addr) for name, addr in email.utils.getaddresses([raw])]


def addrs_only(raw: str) -> list[str]:
    return [addr for _, addr in parse_addr_list(raw) if addr]


@dataclass
class ParsedEmail:
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    from_name: str = ""
    from_addr: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: list[dict] = field(default_factory=list)  # [{cid, filename, content_type, content}]
    sent_at: Optional[datetime] = None
    is_bounce: bool = False
    bounced_addr: str = ""
    bounce_reason: str = ""


def _clean_angle_id(raw: str) -> str:
    return raw.strip().strip("<>")


MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024   # 单张内嵌图上限 2MB
MAX_ATTACHMENTS = 20


def _extract_content(msg: Message) -> tuple[str, str, list[dict]]:
    """提取 (纯文本, 原始HTML, 内嵌图片列表)。

    内嵌图片 = image/* 且带 Content-ID 的部件（正文以 cid: 引用）。
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []

    def walk(part: Message) -> None:
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", ""))
        if "attachment" in disp:
            return
        if part.is_multipart():
            for sub in part.get_payload():
                walk(sub)
            return
        payload = part.get_payload(decode=True)
        if payload is None:
            return
        if ctype.startswith("image/") and part.get("Content-ID") and len(attachments) < MAX_ATTACHMENTS:
            data = payload if len(payload) <= MAX_ATTACHMENT_BYTES else b""
            if data:
                cid = _clean_angle_id(part.get("Content-ID", ""))
                filename = decode_header_value(part.get_filename() or "") or f"{cid}"
                attachments.append({
                    "cid": cid, "filename": filename,
                    "content_type": ctype, "content": data,
                })
            return
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            plain_parts.append(text)
        elif ctype == "text/html":
            html_parts.append(text)

    walk(msg)

    html = "\n".join(html_parts).strip()

    if plain_parts:
        text = "\n".join(plain_parts).strip()
    elif html:
        stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
        stripped = re.sub(r"<br\s*/?>", "\n", stripped, flags=re.I)
        stripped = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", stripped, flags=re.I)
        stripped = re.sub(r"<[^>]+>", "", stripped)
        text = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    else:
        text = ""
    return text, html, attachments


def _detect_bounce(msg: Message, body_text: str) -> tuple[bool, str, str]:
    """识别退信通知，返回 (是否退信, 被退回的邮箱, 原因)。"""
    reason = ""
    subject = decode_header_value(msg.get("Subject", ""))
    sub_low = subject.lower()

    # multipart/report; report-type=delivery-status 是标准 NDR
    ctype = msg.get_content_type()
    if ctype == "multipart/report":
        reason = "delivery-status report"
    elif any(k in sub_low for k in ("delivery status notification", "undelivered",
                                      "returned mail", "failure notice", "退信", "邮件被退回")):
        reason = "bounce subject"

    if not reason and not any(k in sub_low for k in ("delivery status", "undelivered",
                                                      "returned mail", "failure notice",
                                                      "退信", "邮件被退回")):
        return False, "", ""
    if not reason:
        reason = "bounce subject"

    # 找被退回的地址：优先 Final-Recipient/Original-Recipient 头
    bounced = ""
    detail_reason = ""

    def walk(part: Message) -> None:
        nonlocal bounced, detail_reason
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    if isinstance(sub, Message):
                        for hdr in ("Final-Recipient", "Original-Recipient"):
                            val = sub.get(hdr)
                            if val and ";" in val and not bounced:
                                bounced = val.split(";")[-1].strip()
                        status = sub.get("Status")
                        diag = sub.get("Diagnostic-Code")
                        if status or diag:
                            parts = [p for p in (status, diag) if p]
                            detail_reason = "; ".join(str(p)[:150] for p in parts)
        elif part.is_multipart():
            for sub in part.get_payload():
                if isinstance(sub, Message):
                    walk(sub)

    if msg.is_multipart():
        walk(msg)
    elif ctype == "message/delivery-status":
        walk(msg)

    if detail_reason:
        reason = detail_reason

    if not bounced:
        # 兜底：从正文里抓邮箱地址，排除发信系统自身
        candidates = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", body_text or subject)
        for c in candidates:
            if not c.lower().endswith(("postmaster", "mailer-daemon")):
                bounced = c
                break

    return True, bounced, reason[:300]


def parse_email_bytes(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw)
    body_text, body_html, attachments = _extract_content(msg)

    from_pairs = parse_addr_list(msg.get("From", ""))
    from_name, from_addr = (from_pairs[0] if from_pairs else ("", ""))

    refs_raw = decode_header_value(msg.get("References", ""))
    references = [_clean_angle_id(x) for x in re.findall(r"<[^>]+>", refs_raw)]

    sent_at = None
    date_raw = msg.get("Date")
    if date_raw:
        try:
            sent_at = email.utils.parsedate_to_datetime(date_raw)
        except Exception:
            sent_at = None

    parsed = ParsedEmail(
        message_id=_clean_angle_id(msg.get("Message-ID", "")),
        in_reply_to=_clean_angle_id(msg.get("In-Reply-To", "")),
        references=references,
        from_name=from_name,
        from_addr=from_addr,
        to=addrs_only(msg.get("To", "")),
        cc=addrs_only(msg.get("Cc", "")),
        subject=decode_header_value(msg.get("Subject", "")),
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        sent_at=sent_at,
    )

    is_bounce, bounced_addr, bounce_reason = _detect_bounce(msg, body_text)
    parsed.is_bounce = is_bounce
    parsed.bounced_addr = bounced_addr
    parsed.bounce_reason = bounce_reason
    return parsed


def normalize_subject(subject: str) -> str:
    """去掉 Re:/回复: 等前缀并归一化，用于线程聚合与回复兜底匹配。"""
    s = subject.strip().lower()
    changed = True
    while changed:
        changed = False
        for prefix in config.REPLY_SUBJECT_PREFIXES:
            if s.startswith(prefix):
                s = s[len(prefix):].strip()
                changed = True
    return re.sub(r"\s+", " ", s)


def render_template(text: str, variables: dict) -> str:
    """把 {{key}} 占位符替换为 variables 中的值，缺失的保留原样。"""
    def repl(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))
    return re.sub(r"\{\{\s*([\w\u4e00-\u9fff]+)\s*\}\}", repl, text)


def extract_placeholders(text: str) -> list[str]:
    """提取模板中用到的占位符名，去重保序。"""
    seen: list[str] = []
    for m in re.finditer(r"\{\{\s*([\w\u4e00-\u9fff]+)\s*\}\}", text):
        key = m.group(1).strip()
        if key not in seen:
            seen.append(key)
    return seen
