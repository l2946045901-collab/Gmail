"""模块七：邮件模板库（创建/分类/变量替换/预览）。"""

from typing import Optional

from .. import db
from ..core.parser import extract_placeholders, render_template


def create_template(name: str, subject: str, body: str, category: str = "通用") -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO templates (name, category, subject, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, category, subject, body, db.now_iso(), db.now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_template(template_id: int, **fields) -> bool:
    allowed = {"name", "category", "subject", "body"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return False
    fields["updated_at"] = db.now_iso()
    keys = ", ".join(f"{k} = ?" for k in fields)
    conn = db.connect()
    try:
        cur = conn.execute(f"UPDATE templates SET {keys} WHERE id = ?", (*fields.values(), template_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_template(template_id: int) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_template(template_id: int) -> Optional[dict]:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_templates(category: Optional[str] = None) -> list[dict]:
    conn = db.connect()
    try:
        if category:
            rows = conn.execute(
                "SELECT * FROM templates WHERE category = ? ORDER BY updated_at DESC", (category,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM templates ORDER BY category, updated_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_categories() -> list[str]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT DISTINCT category FROM templates ORDER BY category").fetchall()
        return [r["category"] for r in rows]
    finally:
        conn.close()


def preview(template_id: int, variables: dict) -> Optional[dict]:
    """预览变量替换后的主题与正文。"""
    tpl = get_template(template_id)
    if not tpl:
        return None
    return {
        "subject": render_template(tpl["subject"], variables),
        "body": render_template(tpl["body"], variables),
        "placeholders": extract_placeholders(tpl["subject"] + tpl["body"]),
    }
