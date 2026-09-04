"""Web 入口。python -m mail_center.web 或 python mailcenter.py web 启动。"""

from ..engine import scheduler
from .app import create_app


def run(host: str = "127.0.0.1", port: int = 8000, start_scheduler: bool = True) -> None:
    from .. import db
    db.init_db()
    if start_scheduler:
        scheduler.start(foreground=False)  # 后台守护线程：轮询收件箱 + 驱动发送队列
    app = create_app()
    print(f"邮件中心已启动 → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
