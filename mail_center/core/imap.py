"""IMAP 收取：会话式连接管理。

设计原则：
  - 拉取时不修改服务器 \\Seen 标志，由调用方在成功入库后再标记
  - 按 INTERNALDATE 排序，优先处理最新邮件（不依赖 UID 顺序）
"""

import imaplib
import re
import time
from dataclasses import dataclass
from typing import Optional

from .parser import ParsedEmail, parse_email_bytes


@dataclass
class ImapConfig:
    host: str
    port: int
    username: str
    password: str


class ImapError(Exception):
    pass


def _internaldate_to_ts(date_str: str) -> float:
    """把 IMAP INTERNALDATE（如 '4-Sep-2026 20:08:10 +0800'）解析成时间戳。

    imaplib.Internaldate2tuple 要求日字段为零填充（'04-Sep-...'），
    但服务器常返回非零填充形式，需预处理。解析失败返回 0（排到最旧）。
    """
    if not date_str:
        return 0.0
    try:
        day, month, rest = date_str.split("-", 2)
        normalized = f"{int(day):02d}-{month}-{rest}"
        tup = imaplib.Internaldate2tuple(f'(INTERNALDATE "{normalized}")'.encode("ascii"))
        if not tup:
            return 0.0
        return time.mktime(tup)
    except Exception:  # noqa: BLE001
        return 0.0


def _item_meta(item) -> str:
    """从 IMAP fetch 响应项中提取元数据字符串。

    imaplib 对不含字面量的响应行返回纯 bytes，对含字面量（如正文）的
    响应返回 (meta_bytes, literal_bytes) 元组。这里统一取元数据部分。
    """
    if isinstance(item, tuple) and item:
        head = item[0]
        return head.decode("ascii", errors="replace") if isinstance(head, (bytes, bytearray)) else ""
    if isinstance(item, (bytes, bytearray)):
        return item.decode("ascii", errors="replace")
    return ""


class ImapSession:
    """保持打开的 IMAP 连接，供一次轮询周期内多次操作。"""

    def __init__(self, cfg: ImapConfig, folder: str = "INBOX"):
        self._cfg = cfg
        self._folder = folder
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._open()

    def _open(self) -> None:
        cfg = self._cfg
        try:
            conn = imaplib.IMAP4_SSL(cfg.host, cfg.port)
            conn.login(cfg.username, cfg.password)
            status, _ = conn.select(self._folder)
            if status != "OK":
                raise ImapError(f"无法打开文件夹 {self._folder}")
            self._conn = conn
        except imaplib.IMAP4.error as e:
            raise ImapError(f"IMAP 登录失败: {e}") from e
        except OSError as e:
            raise ImapError(f"无法连接 {cfg.host}:{cfg.port} — {e}") from e

    # ------------------------------------------------------------------
    # 拉取
    # ------------------------------------------------------------------

    def fetch_unseen(self, limit: int = 50) -> list[tuple[int, ParsedEmail]]:
        """拉取未读邮件，按到达时间倒序（新邮件优先）。

        返回 [(uid, parsed), ...]。不修改已读标志——
        调用方成功入库后调用 mark_seen()。
        """
        conn = self._require_conn()
        status, data = conn.uid("search", None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        # 按 INTERNALDATE 排序：分批批量取（单条命令 UID 列表过长会被服务器拒绝）
        date_map: dict[int, str] = {}
        CHUNK = 150
        for i in range(0, len(uids), CHUNK):
            chunk = uids[i:i + CHUNK]
            try:
                status, flag_data = conn.uid("fetch", b",".join(chunk), "(INTERNALDATE)")
            except Exception:  # noqa: BLE001
                status, flag_data = "NO", None
            if status == "OK" and flag_data:
                for item in flag_data:
                    meta = _item_meta(item)
                    if not meta:
                        continue
                    m = re.search(r"UID (\d+)", meta)
                    d = re.search(r'INTERNALDATE "([^"]+)"', meta)
                    if m and d:
                        date_map[int(m.group(1))] = d.group(1)

        def sort_key(u: bytes) -> float:
            # 日期解析失败时回退 UID 顺序（新邮件 UID 通常更大）
            ts = _internaldate_to_ts(date_map.get(int(u), ""))
            return ts if ts else float(int(u)) / 1e12

        uids.sort(key=sort_key, reverse=True)  # 新的在前
        uids = uids[:limit]

        results: list[tuple[int, ParsedEmail]] = []
        for uid in uids:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = None
            for item in msg_data:
                if isinstance(item, tuple) and len(item) > 1:
                    raw = item[1]
                    break
            if raw is None:
                continue
            parsed = parse_email_bytes(raw)
            results.append((int(uid), parsed))
        return results

    def fetch_recent(self, limit: int = 30) -> list[tuple[int, bool, ParsedEmail]]:
        """拉取最近 N 封邮件（含已读），用于一次性全量同步。

        按 INTERNALDATE 排序取最新（不依赖 UID 顺序，兼容 QQ 等 UID 不连续的服务器）。
        返回 [(uid, is_seen, parsed), ...]，按时间倒序。
        """
        conn = self._require_conn()
        status, data = conn.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        all_uids = data[0].split()
        if not all_uids:
            return []

        # 分批取 INTERNALDATE + FLAGS（单条命令 UID 列表过长会被服务器拒绝）
        meta_map: dict[int, tuple[str, bool]] = {}
        CHUNK = 150
        for i in range(0, len(all_uids), CHUNK):
            chunk = all_uids[i:i + CHUNK]
            try:
                status, meta_data = conn.uid("fetch", b",".join(chunk), "(INTERNALDATE FLAGS)")
            except Exception:  # noqa: BLE001
                status, meta_data = "NO", None
            if status == "OK" and meta_data:
                for item in meta_data:
                    meta = _item_meta(item)
                    if not meta:
                        continue
                    m = re.search(r"UID (\d+)", meta)
                    d = re.search(r'INTERNALDATE "([^"]+)"', meta)
                    if not m:
                        continue
                    uid_int = int(m.group(1))
                    date_str = d.group(1) if d else ""
                    is_seen = "\\Seen" in meta
                    meta_map[uid_int] = (date_str, is_seen)

        # 按到达时间倒序取最新 limit 封
        ordered = sorted(meta_map.keys(), key=lambda u: _internaldate_to_ts(meta_map[u][0]), reverse=True)[:limit]

        results: list[tuple[int, bool, ParsedEmail]] = []
        for uid_int in ordered:
            uid = str(uid_int).encode()
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw = None
            for item in msg_data:
                if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray)):
                    raw = item[1]
                    break
            if raw is None:
                continue
            is_seen = meta_map[uid_int][1]
            parsed = parse_email_bytes(raw)
            results.append((uid_int, is_seen, parsed))
        return results

    # ------------------------------------------------------------------
    # 状态操作
    # ------------------------------------------------------------------

    def mark_seen(self, uid: int) -> None:
        conn = self._require_conn()
        conn.uid("store", str(uid), "+FLAGS", "(\\Seen)")

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _require_conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise ImapError("IMAP 会话已关闭")
        return self._conn

    def __enter__(self) -> "ImapSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 兼容旧接口
# ---------------------------------------------------------------------------

def test_connection(cfg: ImapConfig) -> None:
    """验证 IMAP 登录，失败抛 ImapError。"""
    try:
        conn = imaplib.IMAP4_SSL(cfg.host, cfg.port)
        conn.login(cfg.username, cfg.password)
        conn.logout()
    except imaplib.IMAP4.error as e:
        raise ImapError(f"IMAP 登录失败: {e}") from e
    except OSError as e:
        raise ImapError(f"无法连接 {cfg.host}:{cfg.port} — {e}") from e


def fetch_unseen(cfg: ImapConfig, limit: int = 50, folder: str = "INBOX") -> list[ParsedEmail]:
    """兼容接口：拉取未读邮件。新代码请使用 ImapSession。"""
    with ImapSession(cfg, folder) as session:
        return [parsed for _, parsed in session.fetch_unseen(limit)]
