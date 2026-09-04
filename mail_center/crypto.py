"""邮箱密码的对称加密（Fernet）。

密钥保存在数据目录下的 .secret_key，首次使用时自动生成。
密钥文件权限应由使用者自行保护（勿提交到版本库）。
"""

from pathlib import Path

from cryptography.fernet import Fernet

from . import config


def _key_file() -> Path:
    return config.KEY_FILE


def _load_fernet() -> Fernet:
    config.ensure_dirs()
    path = _key_file()
    if path.exists():
        key = path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        path.write_bytes(key)
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    """加密，返回 base64 字符串。"""
    return _load_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """解密，返回明文。"""
    return _load_fernet().decrypt(token.encode("ascii")).decode("utf-8")
