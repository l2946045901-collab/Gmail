"""模块二：普邮管理（点对点发送、会话线程、统一收件箱）。"""

import email.utils
from typing import Optional

from .. import crypto, db
from ..core import smtp as core_smtp
from ..core.parser import normalize_subject, render_template
from . import accounts as accounts_svc
from . import blacklist as blacklist_svc
from . import templates as templates_svc


class DirectMailError(Exception):
    pass


def _append_signature(body: str, signature: str) -> str:
    if not signature:
        return body
    return f"{body}\n\n--\n{signature}"


def send_direct(
    account_id: int,
    to: str,
    subject: str,
    body: str,
    profile_id: Optional[int] = None,
    cc: Optional[str] = None,
    template_id: Optional[int] = None,
    variables: Optional[dict] = None,
) -> dict:
    """点对点发送。命中黑名单时拒绝发送。返回结果字典。"""
    acc = accounts_svc.get_account(account_id)
    if not acc:
        raise DirectMailError("发件账号不存在")
    if not acc["active"]:
        raise DirectMailError("该邮箱已被停用")

    # 黑名单校验（主送 + 抄送）
    targets = [to] + ([c.strip() for c in cc.split(",") if c.strip()] if cc else [])
    for addr in targets:
        if blacklist_svc.contains(addr):
            raise DirectMailError(f"收件人 {addr} 在黑名单中，已拦截发送。如需发送请先移出黑名单")

    # 使用模板时先渲染
    if template_id:
        tpl = templates_svc.get_template(template_id)
        if not tpl:
            raise DirectMailError("模板不存在")
        variables = variables or {}
        subject = render_template(subject or tpl["subject"], variables)
        body = render_template(body or tpl["body"], variables)

    display_name, signature = accounts_svc.resolve_sender(account_id, profile_id)
    final_body = _append_signature(body, signature)

    msg = core_smtp.build_message(
        from_display=display_name,
        from_addr=acc["email"],
        to_addrs=[to],
        subject=subject,
        body=final_body,
        cc_addrs=[c.strip() for c in cc.split(",") if c.strip()] if cc else None,
    )

    password = crypto.decrypt_secret(acc["password_enc"])
    cfg = core_smtp.SmtpConfig(
        host=acc["smtp_host"], port=acc["smtp_port"], use_ssl=bool(acc["smtp_ssl"]),
        username=acc["username"], password=password,
    )

    now = db.now_iso()
    conn = db.connect()
    try:
        try:
            core_smtp.send_message(cfg, msg, envelope_from=acc["email"])
            status, error = "sent", ""
        except core_smtp.SmtpSendError as e:
            status, error = "failed", str(e)

        message_id = msg["Message-ID"] or ""
        # 记录邮件本体
        conn.execute(
            """INSERT INTO messages
               (account_id, direction, message_id, from_addr, to_addrs, cc_addrs,
                subject, body_text, sent_at, is_read, created_at)
               VALUES (?, 'out', ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                account_id, message_id, acc["email"], to, cc or "", subject, final_body, now, now,
            ),
        )
        # 发送日志
        conn.execute(
            """INSERT INTO send_logs
               (account_id, direction_src, to_addr, subject, status, error, sent_at)
               VALUES (?, 'direct', ?, ?, ?, ?, ?)""",
            (account_id, to, subject, status, error, now),
        )
        conn.commit()
    finally:
        conn.close()

    if status == "failed":
        raise DirectMailError(f"发送失败：{error}")
    return {"ok": True, "to": to, "subject": subject, "message_id": message_id}


def inbox(account_id: Optional[int] = None, unread_only: bool = False,
          limit: int = 50) -> list[dict]:
    """统一收件箱：所有账号的来信，可按账号/未读过滤。"""
    conn = db.connect()
    try:
        sql = """SELECT m.*, a.email AS account_email, a.name AS account_name
                 FROM messages m JOIN accounts a ON a.id = m.account_id
                 WHERE m.direction = 'in'"""
        params: list = []
        if account_id:
            sql += " AND m.account_id = ?"
            params.append(account_id)
        if unread_only:
            sql += " AND m.is_read = 0"
        sql += " ORDER BY COALESCE(m.sent_at, m.received_at, m.created_at) DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_read(message_id_pk: int) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (message_id_pk,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def conversations(account_id: Optional[int] = None, limit: int = 100) -> list[dict]:
    """会话列表：按往来对象邮箱聚合，显示最新消息摘要。"""
    conn = db.connect()
    try:
        # 出站：以 to_addrs 为对象；入站：以 from_addr 为对象
        sql = """
            SELECT peer, MAX(sort_time) AS last_time, COUNT(*) AS msg_count,
                   MAX(subject) AS last_subject
            FROM (
                SELECT to_addrs AS peer,
                       COALESCE(sent_at, created_at) AS sort_time,
                       subject
                FROM messages WHERE direction = 'out'
                  AND (? IS NULL OR account_id = ?)
                UNION ALL
                SELECT from_addr AS peer,
                       COALESCE(sent_at, received_at, created_at) AS sort_time,
                       subject
                FROM messages WHERE direction = 'in' AND is_bounce = 0
                  AND (? IS NULL OR account_id = ?)
            )
            WHERE peer != ''
            GROUP BY peer
            ORDER BY last_time DESC
            LIMIT ?
        """
        rows = conn.execute(sql, (account_id, account_id, account_id, account_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def conversation_thread(peer: str, account_id: Optional[int] = None) -> list[dict]:
    """单个会话的全部往来邮件，按时间正序。"""
    conn = db.connect()
    try:
        sql = """SELECT m.*, a.email AS account_email
                 FROM messages m JOIN accounts a ON a.id = m.account_id
                 WHERE (
                    (m.direction = 'out' AND m.to_addrs = ?)
                    OR (m.direction = 'in' AND m.from_addr = ?)
                 ) AND (? IS NULL OR m.account_id = ?)
                 ORDER BY COALESCE(m.sent_at, m.received_at, m.created_at) ASC"""
        rows = conn.execute(sql, (peer, peer, account_id, account_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reply(account_id: int, peer: str, body: str,
          profile_id: Optional[int] = None) -> dict:
    """在会话内回复：自动取最近一封往来邮件的主题加 Re: 前缀。"""
    thread = conversation_thread(peer, account_id)
    if thread:
        last_subject = thread[-1]["subject"]
        norm = normalize_subject(last_subject)
        subject = last_subject if last_subject.lower().startswith(("re:", "回复:")) else f"Re: {norm or last_subject}"
    else:
        subject = "Re:"
    return send_direct(account_id, peer, subject, body, profile_id=profile_id)
