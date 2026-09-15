"""应用配置

站点通用选项、功能开关与 HTTPS/HSTS 设置来自项目根目录的 config.yml
（可用环境变量 CLOUDPAN_CONFIG 指定其他路径），未配置的项回退到本文件的默认值。
"""
import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CLOUDPAN_CONFIG", str(BASE_DIR / "config.yml")))

# 项目名：指向开源项目本身，固定标识；网站名（SITE_NAME）可由部署者自定义
PROJECT_NAME = "创想云盘"
PROJECT_NAME_EN = "CX-Drive"

# config.yml 中 features 下的键名 -> app.config 键名
FEATURE_KEYS = {
    "allow_register": "ALLOW_REGISTER",
    "enable_preview": "ENABLE_PREVIEW",
    "enable_share": "ENABLE_SHARE",
    "enable_remote": "ENABLE_REMOTE",
}

# HSTS 默认有效期：1 年（秒）
HSTS_MAX_AGE = 31536000


def _resolve_path(value):
    """证书路径：相对路径按项目根目录解析，空值返回 None"""
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _resolve_https(https):
    """返回 (是否启用 HTTPS, 证书路径, 私钥路径)

    开关已打开但证书、私钥缺失时自动关闭 HTTPS：宁可以 HTTP 启动，
    也不要让服务因证书问题起不来。
    """
    certfile = _resolve_path(https.get("certfile"))
    keyfile = _resolve_path(https.get("keyfile"))
    if not https.get("enabled"):
        return False, certfile, keyfile
    if not certfile or not keyfile:
        print("警告：已开启 HTTPS 但未配置 certfile/keyfile，已自动关闭 HTTPS")
        return False, certfile, keyfile
    missing = [p for p in (certfile, keyfile) if not p.is_file()]
    if missing:
        print("警告：证书文件不存在，已自动关闭 HTTPS：" + "、".join(str(p) for p in missing))
        return False, certfile, keyfile
    return True, certfile, keyfile


def load_site_config(path=CONFIG_FILE):
    """读取 config.yml，返回可并入 app.config 的扁平字典

    文件缺失或格式错误时全部回退默认值，保证服务始终能启动。
    """
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        pass
    except yaml.YAMLError as e:
        print(f"警告：{path} 解析失败，改用默认配置（{e}）")
    if not isinstance(data, dict):
        print(f"警告：{path} 顶层结构应为映射，改用默认配置")
        data = {}

    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    site = data.get("site") if isinstance(data.get("site"), dict) else {}
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    https = data.get("https") if isinstance(data.get("https"), dict) else {}

    project_name = str(project.get("name") or "").strip() or PROJECT_NAME
    cfg = {
        "PROJECT_NAME": project_name,
        "PROJECT_NAME_EN": str(project.get("name_en") or "").strip() or PROJECT_NAME_EN,
        # 网站名留空时回退项目名，避免界面出现空白标题
        "SITE_NAME": str(site.get("name") or "").strip() or project_name,
    }
    for yml_key, cfg_key in FEATURE_KEYS.items():
        cfg[cfg_key] = bool(features.get(yml_key, True))

    https_enabled, certfile, keyfile = _resolve_https(https)
    try:
        hsts_max_age = int(https.get("hsts_max_age", HSTS_MAX_AGE))
    except (TypeError, ValueError):
        hsts_max_age = HSTS_MAX_AGE
    if hsts_max_age <= 0:
        hsts_max_age = HSTS_MAX_AGE
    cfg.update({
        "HTTPS_ENABLED": https_enabled,
        "HTTPS_CERTFILE": str(certfile or ""),
        "HTTPS_KEYFILE": str(keyfile or ""),
        # HSTS 依赖 HTTPS，未开启 HTTPS 时强制关闭
        "HSTS_ENABLED": https_enabled and bool(https.get("hsts", True)),
        "HSTS_MAX_AGE": hsts_max_age,
    })
    if https_enabled:
        print(f"HTTPS 已启用（证书：{certfile}），HSTS：{'开启' if cfg['HSTS_ENABLED'] else '关闭'}")
    return cfg


class Config:
    # 站点信息与功能开关：默认值，由 config.yml 覆盖
    PROJECT_NAME = PROJECT_NAME
    PROJECT_NAME_EN = PROJECT_NAME_EN
    SITE_NAME = PROJECT_NAME
    ALLOW_REGISTER = True
    ENABLE_PREVIEW = True
    ENABLE_SHARE = True
    ENABLE_REMOTE = True

    # HTTPS 与 HSTS：默认值，由 config.yml 的 https 段覆盖
    HTTPS_ENABLED = False
    HTTPS_CERTFILE = ""
    HTTPS_KEYFILE = ""
    HSTS_ENABLED = False
    HSTS_MAX_AGE = HSTS_MAX_AGE

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
