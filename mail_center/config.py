"""全局配置：路径与默认参数。"""

import os
from pathlib import Path

# 数据目录：项目根目录下的 data/
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"

DB_PATH = DATA_DIR / "mailcenter.db"
KEY_FILE = DATA_DIR / ".secret_key"

# 收件箱轮询间隔（秒）
INBOX_POLL_INTERVAL = int(os.environ.get("MC_POLL_INTERVAL", "120"))
# 单次轮询每个邮箱最多拉取的新邮件数
POLL_FETCH_LIMIT = 50

# 发信队列轮询间隔（秒）
SEND_WORKER_INTERVAL = 5

# 退信自动加入黑名单的触发次数（同一邮箱累计退信达到该值后自动拉黑）
AUTO_BLACKLIST_BOUNCE_COUNT = 2

# 未读判定：拉取时 IMAP \Seen 标志未置位的邮件
# 回复匹配兜底：主题相似度归一化时去除的前缀
REPLY_SUBJECT_PREFIXES = ("re:", "回复:", "答复:", "fw:", "fwd:", "转发:")

# 意向评分关键词
INTENT_HIGH_KEYWORDS = [
    "感兴趣", "有兴趣", "可以聊聊", "约个时间", "方便通话", "电话沟通",
    "发一下资料", "发份资料", "报价", "多少钱", "怎么合作", "合作方案",
    "下周", "这周", "明天", "今天", "会议", "见面", "演示", "demo",
    "interested", "let's talk", "schedule a call", "send me", "pricing", "quote",
]
INTENT_MID_KEYWORDS = [
    "了解一下", "先看看", "发来看看", "后续再聊", "暂时不需要", "先不用",
    "保持联系", "tell me more", "keep in touch", "later",
]
INTENT_LOW_KEYWORDS = [
    "不需要", "不感兴趣", "别发了", "取消订阅", "退订", "勿扰",
    "not interested", "unsubscribe", "stop emailing", "remove me",
]

# 常见邮箱服务商预置配置
PROVIDERS = {
    "gmail": {
        "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_ssl": 1,
        "imap_host": "imap.gmail.com", "imap_port": 993,
        "note": "需要在 Google 账号中生成应用专用密码（16 位）",
    },
    "outlook": {
        "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_ssl": 0,
        "imap_host": "outlook.office365.com", "imap_port": 993,
        "note": "使用账号密码或应用密码",
    },
    "qq": {
        "smtp_host": "smtp.qq.com", "smtp_port": 465, "smtp_ssl": 1,
        "imap_host": "imap.qq.com", "imap_port": 993,
        "note": "需要在 QQ 邮箱设置中开启 IMAP/SMTP 并获取授权码",
    },
    "163": {
        "smtp_host": "smtp.163.com", "smtp_port": 465, "smtp_ssl": 1,
        "imap_host": "imap.163.com", "imap_port": 993,
        "note": "需要在网易邮箱设置中开启 IMAP 并设置客户端授权码",
    },
    "aliyun": {
        "smtp_host": "smtp.qiye.aliyun.com", "smtp_port": 465, "smtp_ssl": 1,
        "imap_host": "imap.qiye.aliyun.com", "imap_port": 993,
        "note": "阿里企业邮箱",
    },
    "tencent_exmail": {
        "smtp_host": "smtp.exmail.qq.com", "smtp_port": 465, "smtp_ssl": 1,
        "imap_host": "imap.exmail.qq.com", "imap_port": 993,
        "note": "腾讯企业邮箱，需开启安全登录或使用专用密码",
    },
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
