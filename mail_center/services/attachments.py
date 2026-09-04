"""邮件内嵌图片存储：保存到 data/attachments/<m{pk}>/，cid→文件映射。"""

import json
import re
from pathlib import Path

from .. import config

_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/svg+xml": ".svg", "image/avif": ".avif",
}

_DIR_SAFE = re.compile(r"^m\d+$")
_FILE_SAFE = re.compile(r"^a\d+\.[a-z0-9]+$")


def _dir_for(message_pk: int) -> Path:
    return config.DATA_DIR / "attachments" / f"m{message_pk}"


def save_attachments(message_pk: int, attachments: list[dict]) -> str:
    """写入附件文件与 cid_map.json。返回目录名（失败/无附件返回 ''）。"""
    if not attachments:
        return ""
    d = _dir_for(message_pk)
    d.mkdir(parents=True, exist_ok=True)
    cid_map: dict[str, str] = {}
    for i, att in enumerate(attachments):
        ext = _EXT.get(att.get("content_type", ""), ".bin")
        fname = f"a{i}{ext}"
        try:
            (d / fname).write_bytes(att["content"])
        except OSError:
            continue
        if att.get("cid"):
            cid_map[att["cid"]] = fname
    if not cid_map and not any((d / f"a{i}").exists() for i in range(1)):
        # 无 cid 也无成功落盘的文件则清掉空目录
        try:
            d.rmdir()
        except OSError:
            pass
        return ""
    (d / "cid_map.json").write_text(json.dumps(cid_map, ensure_ascii=False), encoding="utf-8")
    return d.name


def load_cid_map(message_pk: int) -> dict[str, str]:
    f = _dir_for(message_pk) / "cid_map.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def resolve(message_pk: int, filename: str) -> Path | None:
    """安全解析附件文件路径（防目录穿越）。"""
    dir_name = f"m{message_pk}"
    if not _DIR_SAFE.match(dir_name) or not _FILE_SAFE.match(filename):
        return None
    path = config.DATA_DIR / "attachments" / dir_name / filename
    return path if path.is_file() else None


def has_attachments(message_pk: int) -> bool:
    return _dir_for(message_pk).is_dir()


def delete_attachments(message_pk: int) -> None:
    d = _dir_for(message_pk)
    if not d.is_dir():
        return
    for f in d.iterdir():
        try:
            f.unlink()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass
