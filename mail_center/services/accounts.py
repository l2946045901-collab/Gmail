"""模块一 + 模块六：邮箱管理（绑定自有邮箱）与发件人配置（显示名/签名）。"""

from typing import Optional

from .. import crypto, db
from ..core import imap as core_imap
from ..core import smtp as core_smtp


def add_account(
    name: str,
    email: str,
    provider: str,
    username: str,
    password: str,
    smtp_host: str,
    smtp_port: int,
    smtp_ssl: bool,
    imap_host: str,
    imap_port: int,
    display_name: str = "",
    signature: str = "",
    daily_limit: int = 300,
    send_interval: int = 30,
) -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO accounts
               (name, email, provider, smtp_host, smtp_port, smtp_ssl,
                imap_host, imap_port, username, password_enc,
                display_name, signature, daily_limit, send_interval, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, email, provider, smtp_host, smtp_port, int(smtp_ssl),
                imap_host, imap_port, username, crypto.encrypt_secret(password),
                display_name, signature, daily_limit, send_interval, db.now_iso(), db.now_iso(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_accounts(active_only: bool = True) -> list[dict]:
    conn = db.connect()
    try:
        sql = "SELECT * FROM accounts"
        if active_only:
            sql += " WHERE active = 1"
        rows = conn.execute(sql + " ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_account(account_id: int) -> Optional[dict]:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_account(account_id: int, **fields) -> bool:
    """通用更新。password 字段会被自动加密。"""
    if not fields:
        return False
    if "password" in fields:
        fields["password_enc"] = crypto.encrypt_secret(fields.pop("password"))
    fields["updated_at"] = db.now_iso()
    keys = ", ".join(f"{k} = ?" for k in fields)
    conn = db.connect()
    try:
        cur = conn.execute(
            f"UPDATE accounts SET {keys} WHERE id = ?", (*fields.values(), account_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def deactivate_account(account_id: int) -> bool:
    return update_account(account_id, active=0)


def test_account(account_id: int) -> dict:
    """测试 SMTP + IMAP 连通性，返回 {'smtp': bool, 'imap': bool, 'errors': [...]}。"""
    acc = get_account(account_id)
    if not acc:
        return {"smtp": False, "imap": False, "errors": ["账号不存在"]}

    password = crypto.decrypt_secret(acc["password_enc"])
    errors = []

    smtp_ok = True
    try:
        core_smtp.test_connection(core_smtp.SmtpConfig(
            host=acc["smtp_host"], port=acc["smtp_port"], use_ssl=bool(acc["smtp_ssl"]),
            username=acc["username"], password=password,
        ))
    except core_smtp.SmtpSendError as e:
        smtp_ok = False
        errors.append(f"SMTP: {e}")

    imap_ok = True
    try:
        core_imap.test_connection(core_imap.ImapConfig(
            host=acc["imap_host"], port=acc["imap_port"],
            username=acc["username"], password=password,
        ))
    except core_imap.ImapError as e:
        imap_ok = False
        errors.append(f"IMAP: {e}")

    return {"smtp": smtp_ok, "imap": imap_ok, "errors": errors}


# ---------------------------------------------------------------------------
# 发件人身份（多套署名）
# ---------------------------------------------------------------------------

def add_sender_profile(account_id: int, name: str, display_name: str,
                       signature: str = "", make_default: bool = False) -> int:
    conn = db.connect()
    try:
        if make_default:
            conn.execute("UPDATE sender_profiles SET is_default = 0 WHERE account_id = ?", (account_id,))
        cur = conn.execute(
            "INSERT INTO sender_profiles (account_id, name, display_name, signature, is_default) VALUES (?, ?, ?, ?, ?)",
            (account_id, name, display_name, signature, int(make_default)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_sender_profiles(account_id: int) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sender_profiles WHERE account_id = ? ORDER BY is_default DESC, id",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_sender_profile(profile_id: int) -> Optional[dict]:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM sender_profiles WHERE id = ?", (profile_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_sender(account_id: int, profile_id: Optional[int] = None) -> tuple[str, str]:
    """确定本次发信使用的 (显示名, 签名)。

    优先级：指定身份 > 账号默认身份 > 账号自身配置。
    """
    if profile_id:
        prof = get_sender_profile(profile_id)
        if prof and prof["account_id"] == account_id:
            return prof["display_name"], prof["signature"]

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sender_profiles WHERE account_id = ? AND is_default = 1", (account_id,)
        ).fetchone()
        if row:
            return row["display_name"], row["signature"]
        acc = conn.execute("SELECT display_name, signature FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if acc:
            return acc["display_name"], acc["signature"]
        return "", ""
    finally:
        conn.close()


def delete_sender_profile(profile_id: int) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM sender_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
