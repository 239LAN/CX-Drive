"""应用配置"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    # 安全密钥，生产环境务必通过环境变量覆盖
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")

    # 数据库
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'cloud_drive.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 文件存储根目录
    STORAGE_ROOT = os.environ.get("STORAGE_ROOT", str(BASE_DIR / "storage"))
    # 分片上传临时目录
    UPLOAD_TMP_ROOT = os.environ.get("UPLOAD_TMP_ROOT", str(BASE_DIR / "uploads"))
    # 远程下载临时目录
    REMOTE_TMP_ROOT = os.environ.get("REMOTE_TMP_ROOT", str(BASE_DIR / "uploads" / "remote"))
    CHUNK_SIZE = 4 * 1024 * 1024

    # 会话
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7  # 7 天

    # 上传安全
    MAX_CONTENT_LENGTH = None  # 上传上限由应用层按会员权益控制

    # 会员权益（字节单位）
    DEFAULT_STORAGE_QUOTA = 10 * 1024 ** 3       # 免费 10GB
    DEFAULT_MAX_FILE_SIZE = 256 * 1024 ** 2      # 免费 256MB
    DEFAULT_SPEED_LIMIT = 4 * 1024 ** 2          # 免费 4MB/s（字节/秒）
    DEFAULT_MONTHLY_TRAFFIC = 10 * 1024 ** 3     # 免费 10GB/月

    # 到期锁定缓冲期
    LOCK_GRACE_DAYS = 30

    # 分片上传会话有效期（秒）
    UPLOAD_SESSION_TTL = 60 * 60 * 24
