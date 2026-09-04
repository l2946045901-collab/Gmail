"""模块三：群发任务（创建、收件人导入、限速配额、发送队列）。

发送由 send_worker 后台驱动：每次取一个待发送收件人，
检查「账号当日配额」与「发送间隔」后通过 SMTP 发出。
"""

import csv
import io
import json
from datetime import date
from typing import Optional

from .. import crypto, db
from ..core import smtp as core_smtp
from ..core.parser import render_template
from . import accounts as accounts_svc
from . import blacklist as blacklist_svc
from . import templates as templates_svc


class CampaignError(Exception):
    pass


# ---------------------------------------------------------------------------
# 任务创建与收件人导入
# ---------------------------------------------------------------------------

def create_campaign(
    name: str,
    account_id: int,
    subject: str,
    body: str,
    template_id: Optional[int] = None,
    profile_id: Optional[int] = None,
    send_interval: Optional[int] = None,
) -> int:
    acc = accounts_svc.get_account(account_id)
    if not acc:
        raise CampaignError("发件账号不存在")
    if template_id:
        tpl = templates_svc.get_template(template_id)
        if not tpl:
            raise CampaignError("模板不存在")
        subject = subject or tpl["subject"]
        body = body or tpl["body"]
    if not subject or not body:
        raise CampaignError("主题和正文不能为空（可直接指定，或通过模板提供）")

    interval = send_interval if send_interval is not None else acc["send_interval"]
    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO campaigns
               (name, account_id, template_id, profile_id, subject, body, send_interval, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
            (name, account_id, template_id, profile_id, subject, body, interval, db.now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_campaign(campaign_id: int) -> Optional[dict]:
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT c.*, a.email AS account_email, a.name AS account_name
               FROM campaigns c JOIN accounts a ON a.id = c.account_id
               WHERE c.id = ?""",
            (campaign_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_campaigns() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT c.*, a.email AS account_email,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id) AS total,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id AND r.status = 'sent') AS sent_cnt,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id AND r.status = 'replied') AS replied_cnt,
                      (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id = c.id AND r.status = 'bounced') AS bounced_cnt
               FROM campaigns c JOIN accounts a ON a.id = c.account_id
               ORDER BY c.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_recipients_from_csv(campaign_id: int, csv_path: str) -> dict:
    """导入收件人。CSV 需含 email 列，可选 name/company 及其他变量列。

    自动过滤：黑名单邮箱、任务内重复邮箱。
    """
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        content = f.read()
    return add_recipients_from_text(campaign_id, content)


def add_recipients_from_text(campaign_id: int, csv_text: str) -> dict:
    camp = get_campaign(campaign_id)
    if not camp:
        raise CampaignError("任务不存在")
    if camp["status"] not in ("draft", "paused"):
        raise CampaignError("任务进行中或已完成，不能修改收件人。可先暂停任务")

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or "email" not in [c.strip().lower() for c in reader.fieldnames]:
        raise CampaignError("CSV 必须包含 email 列")

    conn = db.connect()
    try:
        existing = {
            r["email"].lower()
            for r in conn.execute("SELECT email FROM recipients WHERE campaign_id = ?", (campaign_id,)).fetchall()
        }
    finally:
        conn.close()

    emails: list[str] = []
    rows: list[dict] = []
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        email_addr = row.get("email", "")
        if not email_addr or "@" not in email_addr:
            continue
        if email_addr.lower() in existing:
            continue
        extra_vars = {k: v for k, v in row.items() if k not in ("email", "name", "company")}
        rows.append({
            "email": email_addr,
            "display_name": row.get("name", ""),
            "company": row.get("company", ""),
            "variables": json.dumps(extra_vars, ensure_ascii=False),
        })
        emails.append(email_addr)

    allowed, blocked = blacklist_svc.filter_out(emails)
    blocked_set = {e.strip().lower() for e in blocked}

    inserted = 0
    conn = db.connect()
    try:
        for r in rows:
            if r["email"].lower() in blocked_set:
                conn.execute(
                    """INSERT OR IGNORE INTO recipients
                       (campaign_id, email, display_name, company, variables, status, skipped_reason)
                       VALUES (?, ?, ?, ?, ?, 'skipped_blacklist', '命中黑名单')""",
                    (campaign_id, r["email"], r["display_name"], r["company"], r["variables"]),
                )
                continue
            conn.execute(
                """INSERT OR IGNORE INTO recipients
                   (campaign_id, email, display_name, company, variables, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (campaign_id, r["email"], r["display_name"], r["company"], r["variables"]),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    return {"imported": inserted, "blocked_by_blacklist": len(blocked_set), "duplicates_skipped": len(emails) - len(set(emails))}


def add_recipient(campaign_id: int, email_addr: str, name: str = "", company: str = "") -> dict:
    if blacklist_svc.contains(email_addr):
        return {"ok": False, "reason": "blacklist"}
    conn = db.connect()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO recipients (campaign_id, email, display_name, company, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (campaign_id, email_addr.strip(), name, company),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 任务状态控制
# ---------------------------------------------------------------------------

def _set_status(campaign_id: int, status: str) -> None:
    conn = db.connect()
    try:
        completed_at = db.now_iso() if status == "completed" else None
        conn.execute(
            "UPDATE campaigns SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
            (status, completed_at, campaign_id),
        )
        conn.commit()
    finally:
        conn.close()


def start_campaign(campaign_id: int) -> None:
    camp = get_campaign(campaign_id)
    if not camp:
        raise CampaignError("任务不存在")
    if camp["status"] == "completed":
        raise CampaignError("任务已完成")
    pending = count_by_status(campaign_id).get("pending", 0)
    if pending == 0:
        raise CampaignError("没有待发送的收件人")
    _set_status(campaign_id, "running")


def pause_campaign(campaign_id: int) -> None:
    camp = get_campaign(campaign_id)
    if not camp:
        raise CampaignError("任务不存在")
    if camp["status"] == "running":
        _set_status(campaign_id, "paused")


def count_by_status(campaign_id: int) -> dict:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM recipients WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 配额与限速
# ---------------------------------------------------------------------------

def sent_today(account_id: int) -> int:
    """该账号今日已发送成功数（普邮 + 群发合计）。"""
    today = date.today().isoformat()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM send_logs WHERE account_id = ? AND status = 'sent' AND sent_at LIKE ?",
            (account_id, f"{today}%"),
        ).fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


def quota_remaining(account_id: int) -> int:
    acc = accounts_svc.get_account(account_id)
    if not acc:
        return 0
    return max(0, acc["daily_limit"] - sent_today(account_id))


def _seconds_since_last_send(account_id: int) -> Optional[float]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT MAX(sent_at) AS t FROM send_logs WHERE account_id = ? AND status = 'sent'",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["t"]:
        return None
    from datetime import datetime
    try:
        last = datetime.fromisoformat(row["t"])
        return (datetime.now().astimezone() - last).total_seconds()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 发送队列：由 send_worker 循环调用
# ---------------------------------------------------------------------------

def send_next_pending() -> dict:
    """处理下一个待发送收件人。

    返回:
      {"action": "sent"/"failed"/"bounced", ...}   发送了一封
      {"action": "wait_interval", "seconds": n}     未到发送间隔
      {"action": "quota_exhausted", "account_id": n,"remaining_quota": 0}
      {"action": "idle"}                            没有运行中任务的待发件
    """
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT r.id AS rid, r.campaign_id, r.email, r.display_name, r.variables,
                      c.subject, c.body, c.account_id, c.send_interval
               FROM recipients r JOIN campaigns c ON c.id = r.campaign_id
               WHERE c.status = 'running' AND r.status = 'pending'
               ORDER BY c.id, r.id LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()

    if not row:
        _complete_finished_campaigns()
        return {"action": "idle"}

    account_id = row["account_id"]
    acc = accounts_svc.get_account(account_id)
    if not acc:
        _fail_recipient(row["rid"], "发件账号不存在")
        return {"action": "failed", "email": row["email"], "error": "发件账号不存在"}

    # 每日配额检查
    if quota_remaining(account_id) <= 0:
        return {"action": "quota_exhausted", "account_id": account_id}

    # 发送间隔检查
    interval = row["send_interval"] or acc["send_interval"]
    if interval > 0:
        elapsed = _seconds_since_last_send(account_id)
        if elapsed is not None and elapsed < interval:
            return {"action": "wait_interval", "seconds": interval - elapsed}

    # 渲染个性化内容
    try:
        variables = json.loads(row["variables"] or "{}")
    except json.JSONDecodeError:
        variables = {}
    variables.setdefault("name", row["display_name"])
    variables.setdefault("company", "")
    subject = render_template(row["subject"], variables)
    body = render_template(row["body"], variables)

    profile_id = None
    camp = get_campaign(row["campaign_id"])
    if camp:
        profile_id = camp["profile_id"]
    display_name, signature = accounts_svc.resolve_sender(account_id, profile_id)
    if signature:
        body = f"{body}\n\n--\n{signature}"

    msg = core_smtp.build_message(
        from_display=display_name, from_addr=acc["email"],
        to_addrs=[row["email"]], subject=subject, body=body,
    )
    message_id = msg["Message-ID"] or ""

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

        recipient_status = "sent" if status == "sent" else "pending"
        conn.execute(
            "UPDATE recipients SET status = ?, message_id = ?, sent_at = ? WHERE id = ?",
            (recipient_status, message_id, now if status == "sent" else None, row["rid"]),
        )
        conn.execute(
            """INSERT INTO messages
               (account_id, direction, message_id, from_addr, to_addrs, subject, body_text,
                sent_at, is_read, campaign_id, recipient_id, created_at)
               VALUES (?, 'out', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (account_id, message_id, acc["email"], row["email"], subject, body,
             now if status == "sent" else None, row["campaign_id"], row["rid"], now),
        )
        conn.execute(
            """INSERT INTO send_logs
               (account_id, direction_src, campaign_id, recipient_id, to_addr, subject, status, error, sent_at)
               VALUES (?, 'campaign', ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, row["campaign_id"], row["rid"], row["email"], subject, status, error, now),
        )
        conn.commit()
    finally:
        conn.close()

    if status == "sent":
        _complete_finished_campaigns()
        return {"action": "sent", "email": row["email"], "message_id": message_id}
    return {"action": "failed", "email": row["email"], "error": error}


def _fail_recipient(recipient_id: int, reason: str) -> None:
    conn = db.connect()
    try:
        conn.execute("UPDATE recipients SET skipped_reason = ? WHERE id = ?", (reason, recipient_id))
        conn.commit()
    finally:
        conn.close()


def _complete_finished_campaigns() -> None:
    """把没有待发件、也没有失败重试项的运行中任务标记为完成。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT c.id FROM campaigns c
               WHERE c.status = 'running'
                 AND NOT EXISTS (SELECT 1 FROM recipients r
                                 WHERE r.campaign_id = c.id AND r.status = 'pending')"""
        ).fetchall()
        now = db.now_iso()
        for r in rows:
            conn.execute("UPDATE campaigns SET status = 'completed', completed_at = ? WHERE id = ?", (now, r["id"]))
        conn.commit()
    finally:
        conn.close()
