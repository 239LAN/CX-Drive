"""通用工具函数"""
import os
import re
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import abort
from flask_login import current_user


def utcnow() -> datetime:
    """当前 UTC 时间（naive）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def gen_storage_key(ext: str = "") -> str:
    """生成物理存储文件名，避免路径穿越"""
    return uuid.uuid4().hex + ext


def safe_filename(name: str) -> str:
    """清洗文件名，去除路径分隔符与危险字符"""
    name = os.path.basename(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    if name in ("", ".", ".."):
        name = "untitled"
    return name[:512]


def human_size(num: int) -> str:
    """字节数转为可读字符串"""
    if num is None:
        return "不限"
    if num < 0:
        num = 0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.2f}{unit}"
        num /= 1024
    return f"{num:.2f}PB"


def human_speed(bps) -> str:
    if bps is None:
        return "不限"
    return human_size(bps) + "/s"
