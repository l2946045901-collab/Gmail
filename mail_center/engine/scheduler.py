"""后台调度器：两个守护线程。

1. 收件箱轮询：周期性调用 inbox.poll_all_accounts()
2. 发送队列：循环调用 campaigns.send_next_pending()，处理限速/配额返回

用标准库 threading 实现，避免引入额外依赖。
"""

import logging
import threading
import time

from .. import config, db
from ..services import campaigns, inbox

log = logging.getLogger("mailcenter.scheduler")

_stop_event = threading.Event()
_threads: list[threading.Thread] = []


def _inbox_loop() -> None:
    log.info("收件箱轮询线程启动，间隔 %ss", config.INBOX_POLL_INTERVAL)
    while not _stop_event.is_set():
        try:
            summary = inbox.poll_all_accounts()
            if summary["new"] or summary["errors"]:
                log.info(
                    "轮询完成：新邮件 %s，回复 %s，退信 %s，错误 %s",
                    summary["new"], summary["replies"], summary["bounces"], len(summary["errors"]),
                )
            for err in summary["errors"]:
                log.warning("轮询错误：%s", err)
        except Exception:  # noqa: BLE001
            log.exception("收件箱轮询异常")
        _stop_event.wait(config.INBOX_POLL_INTERVAL)


def _send_loop() -> None:
    log.info("发送队列线程启动")
    while not _stop_event.is_set():
        try:
            result = campaigns.send_next_pending()
            action = result.get("action")
            if action == "sent":
                log.info("已发送：%s", result.get("email"))
                continue  # 立即处理下一封（间隔由 send_next_pending 内部判定）
            if action == "failed":
                log.warning("发送失败：%s — %s", result.get("email"), result.get("error"))
                _stop_event.wait(1)
                continue
            if action == "wait_interval":
                _stop_event.wait(min(result.get("seconds", 1), 5))
                continue
            if action == "quota_exhausted":
                log.warning("账号 %s 今日配额已用完，暂停发送", result.get("account_id"))
                _stop_event.wait(60)
                continue
            # idle
            _stop_event.wait(config.SEND_WORKER_INTERVAL)
        except Exception:  # noqa: BLE001
            log.exception("发送队列异常")
            _stop_event.wait(5)


def start(foreground: bool = False) -> None:
    """启动调度器。foreground=True 时阻塞主线程（用于 `scheduler run`）。"""
    db.init_db()
    _stop_event.clear()

    targets = [_inbox_loop, _send_loop]
    for t in targets:
        th = threading.Thread(target=t, daemon=True, name=t.__name__)
        th.start()
        _threads.append(th)

    log.info("调度器已启动（收件箱轮询 + 发送队列）")
    if foreground:
        try:
            while not _stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            stop()


def stop() -> None:
    _stop_event.set()
    for th in _threads:
        th.join(timeout=3)
    _threads.clear()
    log.info("调度器已停止")
