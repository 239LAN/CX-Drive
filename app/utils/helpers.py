"""通用工具函数"""
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

from flask import abort, session
from flask_login import current_user


def utcnow() -> datetime:
    """当前 UTC 时间（naive）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_admin_view_user():
    """管理员正在代管的目标用户；未代管返回 None"""
    if not current_user.is_authenticated or not current_user.is_admin:
        return None
    uid = session.get("admin_view_uid")
    if not uid:
        return None
    from app.extensions import db
    from app.models import User
    return db.session.get(User, int(uid))


def current_file_owner():
    """文件操作主体：管理员代管他人文件时为目标用户，否则为当前登录用户"""
    return get_admin_view_user() or current_user


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


# 文件名长度上限，与 File.name 列宽保持一致
MAX_FILENAME_LEN = 512


def safe_filename(name: str) -> str:
    """清洗文件名，去除路径分隔符与危险字符；超长时截断但保留扩展名"""
    name = os.path.basename(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    if name in ("", ".", ".."):
        name = "untitled"
    if len(name) <= MAX_FILENAME_LEN:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext) >= MAX_FILENAME_LEN:
        return name[:MAX_FILENAME_LEN]
    return stem[:MAX_FILENAME_LEN - len(ext)] + ext


def guess_mimetype(filename: str) -> str:
    """按扩展名推断响应类型；未知类型回退为二进制流"""
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def content_disposition(filename: str) -> str:
    """构造带 ASCII 回退名与 RFC 5987 编码名的 Content-Disposition"""
    stem, ext = os.path.splitext(filename)
    ascii_stem = re.sub(r"[^\x20-\x7e]", "", stem).replace('"', "_").replace("\\", "_").strip()
    ascii_ext = re.sub(r"[^\x20-\x7e]", "", ext)
    fallback = (ascii_stem or "download") + ascii_ext
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


# 禁止上传的可执行文件与脚本类扩展名
DANGEROUS_EXTS = {
    # Windows / macOS / Linux 可执行文件与动态库
    ".exe", ".dll", ".com", ".scr", ".msi", ".cpl", ".sys", ".drv", ".so", ".dylib",
    # 脚本与快捷方式
    ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".hta", ".lnk", ".reg", ".wsf",
    ".sh", ".bash", ".py", ".pl", ".rb", ".jar",
    # Web 服务端脚本（避免文件被误部署后执行）
    ".php", ".php3", ".php4", ".php5", ".phtml", ".pht", ".phar",
    ".asp", ".aspx", ".ashx", ".asmx", ".jsp", ".jspx", ".cgi",
}

# 文件头特征：识别伪装成普通文件的程序或脚本
_BINARY_MAGIC = (
    (b"MZ", "Windows 可执行文件"),
    (b"\x7fELF", "Linux 可执行文件"),
    (b"\xca\xfe\xba\xbe", "Java 字节码"),
    (b"\xfe\xed\xfa\xce", "macOS 可执行文件"),
    (b"\xcf\xfa\xed\xfe", "macOS 可执行文件"),
    (b"\xed\xab\xee\xdb", "安装包"),
)
_TEXT_MAGIC = (("<?php", "PHP 脚本"), ("#!", "脚本文件"))

UPLOAD_SNIFF_LEN = 4096


def read_file_head(path: str, length: int = UPLOAD_SNIFF_LEN) -> bytes:
    """读取文件头部若干字节，供内容嗅探使用"""
    with open(path, "rb") as fh:
        return fh.read(length)


def sniff_upload_head(head: bytes) -> str:
    """按文件头判断是否疑似程序/脚本，返回类型描述；未命中返回空串"""
    if not head:
        return ""
    for magic, label in _BINARY_MAGIC:
        if head.startswith(magic):
            return label
    low = head.lower()
    for magic, label in _TEXT_MAGIC:
        if low.startswith(magic.encode()):
            return label
    return ""


def check_extension(filename: str):
    """校验扩展名是否在黑名单内，命中抛 ValueError"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in DANGEROUS_EXTS:
        raise ValueError(f"出于安全考虑，禁止上传 {ext} 类型的文件")


def check_upload_security(filename: str, head: bytes):
    """上传安全校验：扩展名黑名单 + 文件头嗅探，命中抛 ValueError"""
    check_extension(filename)
    label = sniff_upload_head(head)
    if label:
        raise ValueError(f"文件内容疑似{label}，与扩展名不符，已拒绝上传")


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
