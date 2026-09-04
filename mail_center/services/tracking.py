"""模块四 + 模块五的上层：多任务合并统计、未回复清单、意向客户筛选导出。"""

import csv
from typing import Optional

from .. import config, db

_LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低", "none": "无"}
_LEVEL_KEYS = ("high", "medium", "low", "none")


# ---------------------------------------------------------------------------
# 任务统计与合并
# ---------------------------------------------------------------------------

def campaign_stats(campaign_id: int) -> dict:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS n FROM recipients
               WHERE campaign_id = ? GROUP BY status""",
            (campaign_id,),
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}

        camp = conn.execute(
            "SELECT name, status, created_at FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()

        sent = by_status.get("sent", 0) + by_status.get("replied", 0) + by_status.get("bounced", 0)
        replied = by_status.get("replied", 0)
        stats = {
            "campaign_id": campaign_id,
            "name": camp["name"] if camp else "",
            "status": camp["status"] if camp else "",
            "total": sum(by_status.values()),
            "pending": by_status.get("pending", 0),
            "sent": sent,
            "replied": replied,
            "bounced": by_status.get("bounced", 0),
            "skipped_blacklist": by_status.get("skipped_blacklist", 0),
            "reply_rate": round(replied / sent * 100, 1) if sent else 0.0,
        }
        return stats
    finally:
        conn.close()


def merged_stats(campaign_ids: list[int]) -> dict:
    """多个任务合并统计。"""
    all_stats = [campaign_stats(cid) for cid in campaign_ids]
    merged = {
        "campaign_ids": campaign_ids,
        "campaign_count": len(all_stats),
        "total": sum(s["total"] for s in all_stats),
        "sent": sum(s["sent"] for s in all_stats),
        "replied": sum(s["replied"] for s in all_stats),
        "bounced": sum(s["bounced"] for s in all_stats),
        "pending": sum(s["pending"] for s in all_stats),
        "skipped_blacklist": sum(s["skipped_blacklist"] for s in all_stats),
    }
    merged["reply_rate"] = round(merged["replied"] / merged["sent"] * 100, 1) if merged["sent"] else 0.0
    merged["per_campaign"] = all_stats
    return merged


def unreplied_list(campaign_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """已发送但未回复的收件人清单。"""
    conn = db.connect()
    try:
        sql = """SELECT r.email, r.display_name, r.company, r.sent_at, c.name AS campaign_name, c.id AS campaign_id
                 FROM recipients r JOIN campaigns c ON c.id = r.campaign_id
                 WHERE r.status = 'sent'"""
        params: list = []
        if campaign_id:
            sql += " AND r.campaign_id = ?"
            params.append(campaign_id)
        sql += " ORDER BY r.sent_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def bounced_list(campaign_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """退信清单。"""
    conn = db.connect()
    try:
        sql = """SELECT r.email, r.bounce_reason, r.sent_at, c.name AS campaign_name
                 FROM recipients r JOIN campaigns c ON c.id = r.campaign_id
                 WHERE r.status = 'bounced'"""
        params: list = []
        if campaign_id:
            sql += " AND r.campaign_id = ?"
            params.append(campaign_id)
        sql += " ORDER BY r.sent_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def replied_list(campaign_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """回复清单（含回复摘要）。"""
    conn = db.connect()
    try:
        sql = """SELECT r.email, r.display_name, r.replied_at, c.name AS campaign_name,
                        m.subject AS reply_subject, substr(m.body_text, 1, 120) AS reply_excerpt
                 FROM recipients r
                 JOIN campaigns c ON c.id = r.campaign_id
                 LEFT JOIN messages m ON m.id = r.reply_message_id
                 WHERE r.status = 'replied'"""
        params: list = []
        if campaign_id:
            sql += " AND r.campaign_id = ?"
            params.append(campaign_id)
        sql += " ORDER BY r.replied_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 意向客户：查询、手动调整、导出
# ---------------------------------------------------------------------------

def list_leads(level: Optional[str] = None, min_score: Optional[int] = None,
               limit: int = 500) -> list[dict]:
    conn = db.connect()
    try:
        sql = "SELECT * FROM leads WHERE 1=1"
        params: list = []
        if level:
            sql += " AND intent_level = ?"
            params.append(level)
        if min_score is not None:
            sql += " AND intent_score >= ?"
            params.append(min_score)
        sql += " ORDER BY intent_score DESC, latest_reply_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["intent_label"] = _LEVEL_LABELS.get(d["intent_level"], d["intent_level"])
            result.append(d)
        return result
    finally:
        conn.close()


def set_lead_level(email_addr: str, level: str) -> bool:
    if level not in _LEVEL_KEYS:
        return False
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE leads SET intent_level = ?, updated_at = ? WHERE email = ?",
            (level, db.now_iso(), email_addr.strip()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def export_leads_csv(path: Optional[str] = None, level: Optional[str] = None,
                     min_score: Optional[int] = None) -> str:
    """导出意向客户为 CSV，返回文件路径。"""
    leads = list_leads(level=level, min_score=min_score, limit=100000)
    if path is None:
        config.ensure_dirs()
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{level}" if level else ""
        path = str(config.EXPORT_DIR / f"leads{suffix}_{stamp}.csv")

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["邮箱", "姓名", "公司", "意向等级", "意向分数", "回复次数", "最近回复时间"])
        for lead in leads:
            writer.writerow([
                lead["email"], lead["name"], lead["company"],
                lead["intent_label"], lead["intent_score"],
                lead["reply_count"], lead["latest_reply_at"] or "",
            ])
    return path


def export_unreplied_csv(campaign_id: Optional[int] = None, path: Optional[str] = None) -> str:
    rows = unreplied_list(campaign_id=campaign_id, limit=100000)
    if path is None:
        config.ensure_dirs()
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(config.EXPORT_DIR / f"unreplied_{stamp}.csv")

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["邮箱", "姓名", "公司", "发送时间", "所属任务"])
        for r in rows:
            writer.writerow([r["email"], r["display_name"], r["company"],
                             r["sent_at"] or "", r["campaign_name"]])
    return path
