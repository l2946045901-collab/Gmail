"""模块八：黑名单管理（添加/拦截/过滤/自动拉黑）。"""

from typing import Optional

from .. import config, db


def add(email: str, reason: str = "", source: str = "manual") -> bool:
    """加入黑名单。已存在则更新原因。返回是否新增。"""
    email = email.strip()
    if not email:
        return False
    conn = db.connect()
    try:
        existing = conn.execute("SELECT id FROM blacklist WHERE email = ?", (email,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE blacklist SET reason = ?, source = ? WHERE id = ?",
                (reason or "（未填写）", source, existing["id"]),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO blacklist (email, reason, source, created_at) VALUES (?, ?, ?, ?)",
            (email, reason, source, db.now_iso()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove(email: str) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM blacklist WHERE email = ?", (email.strip(),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def contains(email: str) -> bool:
    conn = db.connect()
    try:
        row = conn.execute("SELECT 1 FROM blacklist WHERE email = ?", (email.strip(),)).fetchone()
        return row is not None
    finally:
        conn.close()


def filter_out(emails: list[str]) -> tuple[list[str], list[str]]:
    """把列表分成 (可发送, 黑名单命中)。"""
    conn = db.connect()
    try:
        rows = conn.execute("SELECT email FROM blacklist").fetchall()
        banned = {r["email"].lower() for r in rows}
    finally:
        conn.close()
    allowed = [e for e in emails if e.strip().lower() not in banned]
    blocked = [e for e in emails if e.strip().lower() in banned]
    return allowed, blocked


def list_blacklist() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM blacklist ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_bounce(email: str) -> Optional[str]:
    """记录一次退信；累计达到阈值自动拉黑，返回自动拉黑时的原因，否则返回 None。"""
    email = email.strip()
    if not email:
        return None
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO bounce_counts (email, count) VALUES (?, 1) "
            "ON CONFLICT(email) DO UPDATE SET count = count + 1",
            (email,),
        )
        row = conn.execute("SELECT count FROM bounce_counts WHERE email = ?", (email,)).fetchone()
        conn.commit()
        count = row["count"] if row else 1
    finally:
        conn.close()

    if count >= config.AUTO_BLACKLIST_BOUNCE_COUNT and not contains(email):
        reason = f"累计退信 {count} 次，系统自动拉黑"
        add(email, reason=reason, source="auto_bounce")
        return reason
    return None
