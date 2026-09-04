"""模块四/五的引擎：收件箱轮询 + 回复匹配 + 退信处理 + 意向提取。

由 scheduler 周期性调用 poll_all_accounts()。

可靠性设计：
  - 先入库、成功后才在服务器标记已读（失败不丢邮件）
  - 按 message_id 去重，重复拉取安全
"""

import re
from typing import Optional

from .. import config, crypto, db
from ..core import imap as core_imap
from ..core.parser import ParsedEmail, normalize_subject
from . import accounts as accounts_svc
from . import attachments
from . import blacklist as blacklist_svc
from .intent import score_reply


def poll_all_accounts() -> dict:
    """轮询所有启用账号的收件箱。返回汇总。"""
    summary = {"polled": 0, "new": 0, "replies": 0, "bounces": 0, "errors": []}
    for acc in accounts_svc.list_accounts(active_only=True):
        try:
            result = poll_account(acc["id"])
            summary["polled"] += 1
            summary["new"] += result["new"]
            summary["replies"] += result["replies"]
            summary["bounces"] += result["bounces"]
        except core_imap.ImapError as e:
            summary["errors"].append(f"{acc['email']}: {e}")
    return summary


def poll_account(account_id: int) -> dict:
    """拉取单个账号的未读邮件并处理。

    流程：拉取（不标记）→ 逐封入库 → 成功后才标记已读。
    """
    acc = accounts_svc.get_account(account_id)
    if not acc:
        raise core_imap.ImapError("账号不存在")
    password = crypto.decrypt_secret(acc["password_enc"])
    cfg = core_imap.ImapConfig(
        host=acc["imap_host"], port=acc["imap_port"],
        username=acc["username"], password=password,
    )

    result = {"new": 0, "replies": 0, "bounces": 0}
    with core_imap.ImapSession(cfg) as session:
        items = session.fetch_unseen(limit=config.POLL_FETCH_LIMIT)
        for uid, parsed in items:
            if parsed.message_id and _message_exists(account_id, parsed.message_id):
                # 已入库（重复拉取），只补标已读
                session.mark_seen(uid)
                continue
            try:
                msg_pk = _insert_incoming(account_id, parsed)
            except Exception:  # noqa: BLE001
                # 入库失败：不标记已读，下次轮询重试
                continue
            session.mark_seen(uid)
            result["new"] += 1

            if parsed.is_bounce:
                _handle_bounce(msg_pk, parsed)
                result["bounces"] += 1
            else:
                if _match_campaign_reply(msg_pk, parsed):
                    result["replies"] += 1
    return result


def sync_account(account_id: int, limit: int = 200) -> dict:
    """全量同步最近邮件（含已读），用于初始化或恢复遗漏。按 message_id 去重。"""
    acc = accounts_svc.get_account(account_id)
    if not acc:
        raise core_imap.ImapError("账号不存在")
    password = crypto.decrypt_secret(acc["password_enc"])
    cfg = core_imap.ImapConfig(
        host=acc["imap_host"], port=acc["imap_port"],
        username=acc["username"], password=password,
    )

    result = {"new": 0, "replies": 0, "bounces": 0, "backfilled": 0}
    with core_imap.ImapSession(cfg) as session:
        items = session.fetch_recent(limit=limit)
        for uid, is_seen, parsed in items:
            if parsed.message_id and _message_exists(account_id, parsed.message_id):
                # 已存在：若旧代码入库时没存 HTML/图片，则回填
                if parsed.body_html or parsed.attachments:
                    if _backfill_html(account_id, parsed):
                        result["backfilled"] += 1
                continue
            try:
                msg_pk = _insert_incoming(account_id, parsed, is_read=is_seen)
            except Exception:  # noqa: BLE001
                continue
            result["new"] += 1
            if parsed.is_bounce:
                _handle_bounce(msg_pk, parsed)
                result["bounces"] += 1
            else:
                if _match_campaign_reply(msg_pk, parsed):
                    result["replies"] += 1
    return result


def _backfill_html(account_id: int, parsed: ParsedEmail) -> bool:
    """给旧数据补 body_html 与内嵌图片。成功返回 True。"""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, body_html, attachment_dir FROM messages "
            "WHERE account_id = ? AND direction = 'in' AND message_id = ?",
            (account_id, parsed.message_id),
        ).fetchone()
        if not row:
            return False
        if row["body_html"] or row["attachment_dir"]:
            return False  # 已有 HTML 或已存过附件
        msg_pk = row["id"]
        conn.execute("UPDATE messages SET body_html = ? WHERE id = ?",
                     (parsed.body_html, msg_pk))
        conn.commit()
    finally:
        conn.close()
    att_dir = attachments.save_attachments(msg_pk, parsed.attachments)
    if att_dir:
        conn = db.connect()
        try:
            conn.execute("UPDATE messages SET attachment_dir = ? WHERE id = ?", (att_dir, msg_pk))
            conn.commit()
        finally:
            conn.close()
    return bool(parsed.body_html or parsed.attachments)


# ---------------------------------------------------------------------------
# 入库与去重
# ---------------------------------------------------------------------------

def _message_exists(account_id: int, message_id: str) -> bool:
    if not message_id:
        return False
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE account_id = ? AND direction = 'in' AND message_id = ?",
            (account_id, message_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _is_own_outgoing(message_id: str) -> bool:
    """该 Message-ID 是否是我方已发出的邮件（回流副本）。"""
    if not message_id:
        return False
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE direction = 'out' AND message_id = ?",
            (message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _insert_incoming(account_id: int, parsed: ParsedEmail, is_read: bool = False) -> int:
    """插入来信（含 HTML 正文与内嵌图片），返回主键。"""
    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO messages
               (account_id, direction, message_id, in_reply_to, references_hdr,
                from_addr, to_addrs, subject, body_text, body_html, sent_at, received_at,
                is_read, is_bounce, created_at)
               VALUES (?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id, parsed.message_id, parsed.in_reply_to,
                " ".join(parsed.references), parsed.from_addr,
                ", ".join(parsed.to), parsed.subject, parsed.body_text, parsed.body_html,
                db.utc_iso(parsed.sent_at), db.now_iso(), int(is_read), int(parsed.is_bounce), db.now_iso(),
            ),
        )
        msg_pk = cur.lastrowid
        att_dir = attachments.save_attachments(msg_pk, parsed.attachments)
        if att_dir:
            conn.execute("UPDATE messages SET attachment_dir = ? WHERE id = ?", (att_dir, msg_pk))
        conn.commit()
        return msg_pk
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 退信处理
# ---------------------------------------------------------------------------

def _handle_bounce(msg_pk: int, parsed: ParsedEmail) -> None:
    bounced = parsed.bounced_addr
    if not bounced:
        return
    conn = db.connect()
    try:
        conn.execute("UPDATE messages SET bounce_for = ? WHERE id = ?", (bounced, msg_pk))
        # 找到该邮箱在群发任务中的记录，标记 bounced
        rows = conn.execute(
            "SELECT id FROM recipients WHERE email = ? AND status IN ('sent','pending','queued')",
            (bounced,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE recipients SET status = 'bounced', bounce_reason = ? WHERE id = ?",
                (parsed.bounce_reason, r["id"]),
            )
        conn.commit()
    finally:
        conn.close()

    auto_reason = blacklist_svc.record_bounce(bounced)
    # auto_reason 非空表示已自动拉黑，由 CLI 层展示日志即可


# ---------------------------------------------------------------------------
# 回复匹配
# ---------------------------------------------------------------------------

def _match_campaign_reply(msg_pk: int, parsed: ParsedEmail) -> bool:
    """判断来信是否是对某条群发邮件的回复；命中则更新 recipients 与 leads。

    匹配策略（按优先级）：
      1. In-Reply-To / References 命中我方已发送的 Message-ID
      2. 主题归一化 + 发件人邮箱命中该任务收件人
    """
    sender = parsed.from_addr
    if not sender:
        return False
    # 自动回复不算客户回复
    if re.search(r"(postmaster|mailer-daemon|noreply|no-reply)", sender, re.I):
        return False
    # 自己发出的邮件回流（如发给自己）不算回复
    if parsed.message_id and _is_own_outgoing(parsed.message_id):
        return False

    conn = db.connect()
    try:
        recipient = None

        # 策略 1：线程头匹配
        thread_ids = [parsed.in_reply_to] + parsed.references
        thread_ids = [t for t in thread_ids if t]
        for tid in thread_ids:
            row = conn.execute(
                """SELECT r.id, r.campaign_id, r.email FROM recipients r
                   JOIN messages m ON m.recipient_id = r.id
                   WHERE m.message_id = ? AND m.direction = 'out'""",
                (tid,),
            ).fetchone()
            if row:
                recipient = row
                break

        # 策略 2：发件人 + 主题兜底
        if not recipient:
            norm = normalize_subject(parsed.subject)
            rows = conn.execute(
                """SELECT r.id, r.campaign_id, r.email, c.subject AS camp_subject
                   FROM recipients r JOIN campaigns c ON c.id = r.campaign_id
                   WHERE r.email = ? AND r.status IN ('sent','bounced')""",
                (sender,),
            ).fetchall()
            for r in rows:
                if norm and norm == normalize_subject(r["camp_subject"]):
                    recipient = r
                    break
            if not recipient and rows:
                # 同一人只发过一个任务时，直接认定
                if len(rows) == 1:
                    recipient = rows[0]

        if not recipient:
            return False

        # 标记回复（只记首次回复）
        cur = conn.execute(
            """UPDATE recipients SET status = 'replied', reply_message_id = ?, replied_at = ?
               WHERE id = ? AND status != 'replied'""",
            (msg_pk, db.now_iso(), recipient["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    _update_lead(sender, parsed, msg_pk, is_first_reply=True)
    return True


# ---------------------------------------------------------------------------
# 意向客户更新
# ---------------------------------------------------------------------------

def _update_lead(email_addr: str, parsed: ParsedEmail, msg_pk: int, is_first_reply: bool) -> None:
    text = f"{parsed.subject}\n{parsed.body_text}"
    score, level = score_reply(text)

    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM leads WHERE email = ?", (email_addr,)).fetchone()
        now = db.now_iso()
        if row:
            # 意向等级只升不降，分数累加
            new_score = row["intent_score"] + score
            new_level = _max_level(row["intent_level"], level)
            conn.execute(
                """UPDATE leads SET intent_score = ?, intent_level = ?,
                          latest_reply_id = ?, latest_reply_at = ?,
                          reply_count = reply_count + 1, updated_at = ?
                   WHERE id = ?""",
                (new_score, new_level, msg_pk, now, now, row["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO leads (email, name, intent_level, intent_score,
                                      latest_reply_id, latest_reply_at, reply_count, first_seen_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (email_addr, parsed.from_name, level, score, msg_pk, now, now, now),
            )
        conn.commit()
    finally:
        conn.close()


_LEVEL_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _max_level(a: str, b: str) -> str:
    return a if _LEVEL_ORDER.get(a, 0) >= _LEVEL_ORDER.get(b, 0) else b
