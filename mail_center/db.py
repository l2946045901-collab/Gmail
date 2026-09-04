"""SQLite 数据库：连接管理与建表。"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import config

SCHEMA = """
-- 邮箱账号
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    provider      TEXT NOT NULL DEFAULT 'custom',
    smtp_host     TEXT NOT NULL,
    smtp_port     INTEGER NOT NULL DEFAULT 465,
    smtp_ssl      INTEGER NOT NULL DEFAULT 1,
    imap_host     TEXT NOT NULL,
    imap_port     INTEGER NOT NULL DEFAULT 993,
    username      TEXT NOT NULL,
    password_enc  TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    signature     TEXT NOT NULL DEFAULT '',
    daily_limit   INTEGER NOT NULL DEFAULT 300,
    send_interval INTEGER NOT NULL DEFAULT 30,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 发件人身份（同一邮箱的多套署名，发信时按场景选择）
CREATE TABLE IF NOT EXISTS sender_profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    signature    TEXT NOT NULL DEFAULT '',
    is_default   INTEGER NOT NULL DEFAULT 0
);

-- 邮件模板
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT '通用',
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 黑名单
CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    reason     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL
);

-- 邮件（收件箱 + 已发送，统一存储）
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    direction      TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    message_id     TEXT NOT NULL DEFAULT '',
    in_reply_to    TEXT NOT NULL DEFAULT '',
    references_hdr TEXT NOT NULL DEFAULT '',
    from_addr      TEXT NOT NULL DEFAULT '',
    to_addrs       TEXT NOT NULL DEFAULT '',
    cc_addrs       TEXT NOT NULL DEFAULT '',
    subject        TEXT NOT NULL DEFAULT '',
    body_text      TEXT NOT NULL DEFAULT '',
    sent_at        TEXT,
    received_at    TEXT,
    is_read        INTEGER NOT NULL DEFAULT 0,
    is_bounce      INTEGER NOT NULL DEFAULT 0,
    bounce_for     TEXT NOT NULL DEFAULT '',
    campaign_id    INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    recipient_id   INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_account_dir ON messages(account_id, direction);
CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_campaign ON messages(campaign_id);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr);

-- 群发任务
CREATE TABLE IF NOT EXISTS campaigns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    template_id      INTEGER REFERENCES templates(id) ON DELETE SET NULL,
    profile_id       INTEGER REFERENCES sender_profiles(id) ON DELETE SET NULL,
    subject          TEXT NOT NULL,
    body             TEXT NOT NULL,
    send_interval    INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','running','paused','completed')),
    created_at       TEXT NOT NULL,
    completed_at     TEXT
);

-- 群发任务收件人明细
CREATE TABLE IF NOT EXISTS recipients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id    INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    email          TEXT NOT NULL COLLATE NOCASE,
    display_name   TEXT NOT NULL DEFAULT '',
    company        TEXT NOT NULL DEFAULT '',
    variables      TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','queued','sent','bounced','replied','skipped_blacklist')),
    message_id     TEXT NOT NULL DEFAULT '',
    sent_at        TEXT,
    opened_at      TEXT,
    reply_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    replied_at     TEXT,
    bounce_reason  TEXT NOT NULL DEFAULT '',
    skipped_reason TEXT NOT NULL DEFAULT '',
    UNIQUE (campaign_id, email)
);
CREATE INDEX IF NOT EXISTS idx_recipients_campaign ON recipients(campaign_id);
CREATE INDEX IF NOT EXISTS idx_recipients_status ON recipients(status);
CREATE INDEX IF NOT EXISTS idx_recipients_email ON recipients(email);

-- 退信统计（按邮箱累计，用于自动拉黑）
CREATE TABLE IF NOT EXISTS bounce_counts (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    count INTEGER NOT NULL DEFAULT 0
);

-- 意向客户（以邮箱为主键聚合所有群发任务的反馈）
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    intent_level  TEXT NOT NULL DEFAULT 'none'
                  CHECK (intent_level IN ('high','medium','low','none')),
    intent_score  INTEGER NOT NULL DEFAULT 0,
    latest_reply_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    latest_reply_at TEXT,
    reply_count   INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 发送日志（所有外发邮件：普邮 + 群发）
CREATE TABLE IF NOT EXISTS send_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    direction_src TEXT NOT NULL DEFAULT 'direct',
    campaign_id   INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    recipient_id  INTEGER,
    to_addr       TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    error         TEXT NOT NULL DEFAULT '',
    sent_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_send_logs_account_date ON send_logs(account_id, sent_at);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """列级迁移：为旧库补充新增列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "body_html" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN body_html TEXT NOT NULL DEFAULT ''")
    if "attachment_dir" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_dir TEXT NOT NULL DEFAULT ''")


def now_iso() -> str:
    """本地时间的 ISO 字符串（精确到秒）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def utc_iso(dt: Optional[datetime]) -> str:
    """把带时区的 datetime 转成本地 ISO 字符串，None 时返回空串。"""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat(timespec="seconds")
