"""存储点管理：容量统计、均衡分配、本地磁盘 / FTP / SFTP / S3 后端读写

存储点（StoragePoint）是文件实体的物理落点：
- local：本机磁盘目录（可多挂载点）
- ftp：远端 FTP 服务器目录
- sftp：远端 SFTP（SSH）服务器目录
- s3：S3 兼容对象存储（MinIO / OSS 等，支持自定义 Endpoint）

远端存储点（ftp/sftp/s3）读取时先拉取到本地缓存再响应，缓存由后台任务按 TTL 清理。

容量规则：每个存储点必填容量上限，可用空间按容量的 90% 计（占用达 90% 即视为已满）。
写入时在「空间足够的可用存储点」中选择占用率最低者，使各点占用率趋于平均。
"""
import ftplib
import os
import shutil
import tempfile
import time
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.models import File, StoragePoint
from app.utils.helpers import read_file_head, utcnow

# 占用率达该比例即视为已满，同时作为可用空间计算基准
FULL_RATIO = 0.9
# FTP 操作超时（秒）
FTP_TIMEOUT = 30
# FTP 相关异常合集：ftplib.all_errors 本身已是元组，不能再嵌进 except 的元组里
FTP_ERRORS = tuple(ftplib.all_errors) + (OSError,)
# SFTP 连接超时（秒）
SFTP_TIMEOUT = 30
# 支持的存储点类型（管理页表单与校验共用）
STORAGE_KINDS = ("local", "ftp", "sftp", "s3")
# 本地容量探测失败时的兜底值
FALLBACK_CAPACITY = 10 * 1024 ** 3
# 最小容量限制（1MB），避免误填导致无法写入
MIN_CAPACITY = 1024 ** 2


class StorageError(Exception):
    """存储点操作失败"""


class StorageFullError(StorageError):
    """没有可用的存储点空间"""


# ---------------- 容量统计 ----------------

def detect_local_capacity(path: str) -> int:
    """探测本地目录所在磁盘总容量，用于新建存储点的默认容量"""
    target = path
    while target and not os.path.exists(target):
        parent = os.path.dirname(target.rstrip("/\\"))
        if parent == target:
            break
        target = parent
    try:
        total = int(shutil.disk_usage(target or "/").total)
    except OSError:
        return FALLBACK_CAPACITY
    return total or FALLBACK_CAPACITY


def used_bytes(point) -> int:
    """存储点已用字节数（按文件记录聚合，容量不随物理扫描变化）"""
    if point is None:
        return 0
    return int(db.session.query(
        db.func.coalesce(db.func.sum(File.size), 0)
    ).filter(
        File.storage_id == point.id,
        File.is_dir == False,  # noqa: E712
        File.is_lost == False,  # noqa: E712
    ).scalar() or 0)


def usable_capacity(point) -> int:
    """可用容量上限（容量的 90%）"""
    return int(int(point.capacity_bytes or 0) * FULL_RATIO)


def remaining_bytes(point) -> int:
    """存储点剩余可用空间（0 表示已满）"""
    return max(usable_capacity(point) - used_bytes(point), 0)


def is_full(point) -> bool:
    return remaining_bytes(point) <= 0


def stats(point) -> dict:
    """存储点容量概况（管理页与判满共用）"""
    capacity = int(point.capacity_bytes or 0)
    used = used_bytes(point)
    usable = usable_capacity(point)
    return {
        "used": used,
        "capacity": capacity,
        "usable": usable,
        "remaining": max(usable - used, 0),
        "ratio": (used / capacity) if capacity > 0 else 1.0,
        "is_full": used >= usable,
        "files": File.query.filter(
            File.storage_id == point.id, File.is_dir == False,  # noqa: E712
            File.is_lost == False,  # noqa: E712
        ).count(),
    }


# ---------------- 写入点选择 ----------------

def enabled_points():
    return StoragePoint.query.filter_by(enabled=True).order_by(
        StoragePoint.sort, StoragePoint.id).all()


def default_point():
    """历史数据兜底：STORAGE_ROOT 对应的本地存储点"""
    root = current_app.config["STORAGE_ROOT"]
    point = StoragePoint.query.filter_by(kind="local", path=root).first()
    if point is None:
        point = StoragePoint.query.filter_by(kind="local").order_by(
            StoragePoint.sort, StoragePoint.id).first()
    return point


def get_point(storage_id):
    """按 id 取存储点；缺失时回退到默认本地存储点（兼容历史记录）"""
    if storage_id:
        point = db.session.get(StoragePoint, int(storage_id))
        if point is not None:
            return point
    return default_point()


class Allocator:
    """按占用率均衡分配写入点

    预留的字节会累加到占用率中，因此批量规划（如存储点转移）能自然摊平到各点。
    """

    def __init__(self, exclude_ids=()):
        self.exclude = {int(i) for i in exclude_ids}
        self.reserved = {}

    def candidates(self):
        return [p for p in enabled_points() if p.id not in self.exclude]

    def pick(self, need_bytes: int = 0):
        """选出「能装下且占用率最低」的可用存储点"""
        need = max(int(need_bytes or 0), 0)
        best = None
        best_ratio = None
        for point in self.candidates():
            used = used_bytes(point) + self.reserved.get(point.id, 0)
            if used + need > usable_capacity(point):
                continue
            ratio = used / max(int(point.capacity_bytes or 0), 1)
            if best is None or ratio < best_ratio:
                best, best_ratio = point, ratio
        if best is None:
            raise StorageFullError("没有可用的存储点空间")
        self.reserved[best.id] = self.reserved.get(best.id, 0) + need
        return best


def pick_point(need_bytes: int = 0):
    """单次写入的存储点选择"""
    return Allocator().pick(need_bytes)


# ---------------- 本地后端 ----------------

def _local_path(point, storage_key: str) -> str:
    root = point.path
    sub = storage_key[:2]
    d = os.path.join(root, sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, storage_key)


# ---------------- FTP 后端 ----------------

def _cache_path(point, storage_key: str, create: bool = True) -> str:
    root = current_app.config["STORAGE_CACHE_ROOT"]
    d = os.path.join(root, str(point.id), storage_key[:2])
    if create:
        os.makedirs(d, exist_ok=True)
    return os.path.join(d, storage_key)


def _remove_quietly(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _ftp_connect(point) -> ftplib.FTP:
    conn = ftplib.FTP()
    try:
        conn.connect(point.host, int(point.port or 21), timeout=FTP_TIMEOUT)
        conn.login(point.username or "anonymous", point.password or "")
        try:
            conn.encoding = "utf-8"
        except (AttributeError, LookupError):
            pass
        conn.set_pasv(True)
        return conn
    except FTP_ERRORS as e:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise StorageError(f"FTP 连接失败：{e}")


def _ftp_close(conn):
    try:
        conn.quit()
    except Exception:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _ftp_dir(point, storage_key: str) -> str:
    base = (point.remote_dir or "/").rstrip("/")
    return f"{base}/{storage_key[:2]}"


def _ftp_path(point, storage_key: str) -> str:
    return f"{_ftp_dir(point, storage_key)}/{storage_key}"


def _ftp_makedirs(conn, dirpath: str):
    if not dirpath:
        return
    absolute = dirpath.startswith("/")
    cur = "/" if absolute else ""
    for part in [p for p in dirpath.split("/") if p]:
        cur = f"{cur.rstrip('/')}/{part}" if cur else part
        if absolute and not cur.startswith("/"):
            cur = "/" + cur
        try:
            conn.mkd(cur)
        except ftplib.all_errors:
            # 目录已存在（550）时忽略；其他错误会在后续读写时暴露
            pass


def _ftp_fetch(point, storage_key: str) -> str:
    """把 FTP 上的文件拉取到本地缓存并返回缓存路径"""
    cached = _cache_path(point, storage_key)
    if os.path.exists(cached):
        return cached
    conn = _ftp_connect(point)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cached), suffix=".part")
    os.close(fd)
    try:
        with open(tmp, "wb") as out:
            conn.retrbinary("RETR " + _ftp_path(point, storage_key), out.write)
        os.replace(tmp, cached)
        return cached
    except FTP_ERRORS as e:
        _remove_quietly(tmp)
        raise StorageError(f"FTP 读取失败：{e}")
    finally:
        _ftp_close(conn)


def _ftp_store(point, storage_key: str, stream):
    conn = _ftp_connect(point)
    try:
        _ftp_makedirs(conn, _ftp_dir(point, storage_key))
        conn.storbinary("STOR " + _ftp_path(point, storage_key), stream)
    except FTP_ERRORS as e:
        raise StorageError(f"FTP 写入失败：{e}")
    finally:
        _ftp_close(conn)


def _ftp_delete(point, storage_key: str):
    conn = _ftp_connect(point)
    try:
        conn.delete(_ftp_path(point, storage_key))
    except ftplib.error_perm as e:
        # 550 = 文件不存在，视为删除成功
        if "550" not in str(e):
            raise StorageError(f"FTP 删除失败：{e}")
    except FTP_ERRORS as e:
        raise StorageError(f"FTP 删除失败：{e}")
    finally:
        _ftp_close(conn)


# ---------------- SFTP 后端 ----------------

def _sftp_connect(point):
    """建立 SFTP 连接（paramiko，默认 22 端口，用户名 / 密码认证）"""
    try:
        import paramiko
    except ImportError as e:  # 依赖缺失时给出可读提示
        raise StorageError("未安装 paramiko，无法使用 SFTP 存储点") from e
    if not (point.host or "").strip():
        raise StorageError("请填写 SFTP 服务器地址")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=(point.host or "").strip(),
            port=int(point.port or 22),
            username=(point.username or "").strip() or None,
            password=point.password or None,
            timeout=SFTP_TIMEOUT,
            banner_timeout=SFTP_TIMEOUT,
            auth_timeout=SFTP_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        return client
    except Exception as e:  # noqa: BLE001 —— paramiko 异常类型繁多，统一包装
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        raise StorageError(f"SFTP 连接失败：{e}")


def _sftp_close(client):
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


def _sftp_dir(point, storage_key: str) -> str:
    base = (point.remote_dir or "/").rstrip("/")
    return f"{base}/{storage_key[:2]}"


def _sftp_path(point, storage_key: str) -> str:
    return f"{_sftp_dir(point, storage_key)}/{storage_key}"


def _sftp_makedirs(sftp, dirpath: str):
    """逐级创建远端目录（已存在则忽略）"""
    if not dirpath:
        return
    absolute = dirpath.startswith("/")
    cur = "/" if absolute else ""
    for part in [p for p in dirpath.split("/") if p]:
        cur = f"{cur.rstrip('/')}/{part}" if cur else part
        if absolute and not cur.startswith("/"):
            cur = "/" + cur
        try:
            sftp.stat(cur)
        except OSError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def _sftp_fetch(point, storage_key: str) -> str:
    """把 SFTP 上的文件拉取到本地缓存并返回缓存路径"""
    cached = _cache_path(point, storage_key)
    if os.path.exists(cached):
        return cached
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cached), suffix=".part")
    os.close(fd)
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            sftp.get(_sftp_path(point, storage_key), tmp)
        finally:
            sftp.close()
        os.replace(tmp, cached)
        return cached
    except Exception as e:  # noqa: BLE001
        _remove_quietly(tmp)
        raise StorageError(f"SFTP 读取失败：{e}")
    finally:
        _sftp_close(client)


def _sftp_head(point, storage_key: str, length: int) -> bytes:
    """只取远端文件头部若干字节，避免整文件拉取"""
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(_sftp_path(point, storage_key), "rb") as remote:
                return remote.read(length)
        finally:
            sftp.close()
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"SFTP 读取失败：{e}")
    finally:
        _sftp_close(client)


def _sftp_store(point, storage_key: str, stream):
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            _sftp_makedirs(sftp, _sftp_dir(point, storage_key))
            if hasattr(stream, "seek"):
                stream.seek(0)
            with sftp.open(_sftp_path(point, storage_key), "wb") as remote:
                shutil.copyfileobj(stream, remote)
        finally:
            sftp.close()
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"SFTP 写入失败：{e}")
    finally:
        _sftp_close(client)


def _sftp_delete(point, storage_key: str):
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            sftp.remove(_sftp_path(point, storage_key))
        except OSError as e:
            # 文件不存在视为删除成功
            if getattr(e, "errno", None) != 2 and "No such file" not in str(e):
                raise StorageError(f"SFTP 删除失败：{e}")
        finally:
            sftp.close()
    finally:
        _sftp_close(client)


def _sftp_exists(point, storage_key: str) -> bool:
    client = _sftp_connect(point)
    try:
        sftp = client.open_sftp()
        try:
            sftp.stat(_sftp_path(point, storage_key))
            return True
        except OSError:
            return False
        finally:
            sftp.close()
    finally:
        _sftp_close(client)


# ---------------- S3 后端（兼容 MinIO / OSS 等） ----------------

def _s3_client(point):
    """构建 S3 客户端；endpoint 留空则使用 AWS 默认地址"""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as e:
        raise StorageError("未安装 boto3，无法使用 S3 存储点") from e
    cfg = BotoConfig(
        signature_version="s3v4",
        retries={"max_attempts": 3},
        s3={"addressing_style": "path" if point.path_style else "auto"},
    )
    try:
        return boto3.client(
            "s3",
            endpoint_url=(point.endpoint or "").strip() or None,
            aws_access_key_id=(point.access_key or "").strip() or None,
            aws_secret_access_key=(point.secret_key or "").strip() or None,
            region_name=(point.region or "").strip() or None,
            config=cfg,
        )
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 客户端初始化失败：{e}")


def _s3_bucket(point) -> str:
    bucket = (point.bucket or "").strip()
    if not bucket:
        raise StorageError("请填写 S3 Bucket")
    return bucket


def _s3_key(point, storage_key: str) -> str:
    """对象键：<remote_dir 作为前缀>/<分片目录>/<storage_key>"""
    prefix = (point.remote_dir or "").strip().strip("/")
    sub = storage_key[:2]
    return f"{prefix}/{sub}/{storage_key}" if prefix else f"{sub}/{storage_key}"


def _s3_fetch(point, storage_key: str) -> str:
    """把 S3 对象下载到本地缓存并返回缓存路径"""
    cached = _cache_path(point, storage_key)
    if os.path.exists(cached):
        return cached
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cached), suffix=".part")
    os.close(fd)
    try:
        client = _s3_client(point)
        client.download_file(_s3_bucket(point), _s3_key(point, storage_key), tmp)
        os.replace(tmp, cached)
        return cached
    except StorageError:
        _remove_quietly(tmp)
        raise
    except Exception as e:  # noqa: BLE001
        _remove_quietly(tmp)
        raise StorageError(f"S3 读取失败：{e}")


def _s3_head(point, storage_key: str, length: int) -> bytes:
    """用 Range 请求只取对象头部若干字节"""
    try:
        client = _s3_client(point)
        resp = client.get_object(
            Bucket=_s3_bucket(point),
            Key=_s3_key(point, storage_key),
            Range=f"bytes=0-{max(int(length) - 1, 0)}",
        )
        body = resp["Body"]
        try:
            return body.read()
        finally:
            body.close()
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 读取失败：{e}")


def _s3_store(point, storage_key: str, stream):
    try:
        client = _s3_client(point)
        if hasattr(stream, "seek"):
            stream.seek(0)
        client.upload_fileobj(stream, _s3_bucket(point), _s3_key(point, storage_key))
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 写入失败：{e}")


def _s3_delete(point, storage_key: str):
    try:
        _s3_client(point).delete_object(
            Bucket=_s3_bucket(point), Key=_s3_key(point, storage_key))
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"S3 删除失败：{e}")


def _s3_exists(point, storage_key: str) -> bool:
    try:
        client = _s3_client(point)
        client.head_object(Bucket=_s3_bucket(point), Key=_s3_key(point, storage_key))
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------- 统一读写接口 ----------------

def read_path(point, storage_key: str) -> str:
    """返回可直接读取的本地路径；远端存储点会先拉取到本地缓存"""
    point = get_point(point.id if point is not None else None)
    if point is None:
        raise StorageError("尚未配置存储点")
    if point.kind == "local":
        return _local_path(point, storage_key)
    if point.kind == "sftp":
        return _sftp_fetch(point, storage_key)
    if point.kind == "s3":
        return _s3_fetch(point, storage_key)
    return _ftp_fetch(point, storage_key)


def read_head(point, storage_key: str, length: int) -> bytes:
    """读取文件头部若干字节（远端只取所需部分，避免整文件拉取）"""
    point = get_point(point.id if point is not None else None)
    if point is None:
        raise StorageError("尚未配置存储点")
    if point.kind == "local":
        return read_file_head(_local_path(point, storage_key), length)
    if point.kind == "sftp":
        return _sftp_head(point, storage_key, length)
    if point.kind == "s3":
        return _s3_head(point, storage_key, length)
    conn = _ftp_connect(point)
    try:
        sock = conn.transfercmd("RETR " + _ftp_path(point, storage_key))
        buf = b""
        try:
            while len(buf) < length:
                chunk = sock.recv(min(65536, length - len(buf)))
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        try:
            conn.voidresp()
        except ftplib.all_errors:
            pass
        return buf
    except FTP_ERRORS as e:
        raise StorageError(f"FTP 读取失败：{e}")
    finally:
        _ftp_close(conn)


def write_stream(point, storage_key: str, stream):
    """把文件流写入存储点（stream 需位于起始位置）"""
    if point.kind == "local":
        dest = _local_path(point, storage_key)
        with open(dest, "wb") as out:
            shutil.copyfileobj(stream, out)
        return
    if point.kind == "sftp":
        _sftp_store(point, storage_key, stream)
        return
    if point.kind == "s3":
        _s3_store(point, storage_key, stream)
        return
    _ftp_store(point, storage_key, stream)


def write_from_path(point, storage_key: str, src_path: str, move: bool = True):
    """把本地文件写入存储点；move=True 时写入后删除源文件"""
    if point.kind == "local":
        dest = _local_path(point, storage_key)
        if os.path.abspath(src_path) == os.path.abspath(dest):
            return
        if move:
            shutil.move(src_path, dest)
        else:
            shutil.copy2(src_path, dest)
        return
    with open(src_path, "rb") as fh:
        write_stream(point, storage_key, fh)
    if move:
        _remove_quietly(src_path)


def delete_object(point, storage_key: str, drop_cache: bool = True):
    """删除存储点上的文件实体（含本地缓存）"""
    point = get_point(point.id if point is not None else None)
    if point is None:
        return
    if point.kind == "local":
        _remove_quietly(_local_path(point, storage_key))
    elif point.kind == "sftp":
        _sftp_delete(point, storage_key)
    elif point.kind == "s3":
        _s3_delete(point, storage_key)
    else:
        _ftp_delete(point, storage_key)
    if drop_cache:
        _remove_quietly(_cache_path(point, storage_key, create=False))


def delete_object_quietly(point, storage_key: str, drop_cache: bool = True):
    """删除失败不抛异常（用于彻底删除等不应中断的场景）"""
    try:
        delete_object(point, storage_key, drop_cache=drop_cache)
    except StorageError:
        pass


def object_exists(point, storage_key: str) -> bool:
    try:
        point = get_point(point.id if point is not None else None)
        if point is None:
            return False
        if point.kind == "local":
            return os.path.exists(_local_path(point, storage_key))
        if point.kind == "sftp":
            return _sftp_exists(point, storage_key)
        if point.kind == "s3":
            return _s3_exists(point, storage_key)
        conn = _ftp_connect(point)
        try:
            conn.size(_ftp_path(point, storage_key))
            return True
        except ftplib.error_perm as e:
            # 550 = 不存在；SIZE 不被支持时保守认为存在
            return "550" not in str(e)
        finally:
            _ftp_close(conn)
    except StorageError:
        return False


# ---------------- 存储点转移 / 校验 / 缓存清理 ----------------

def transfer_plan(point, files) -> dict:
    """统计把 files 从 point 转移出去所需与可用的空间（可用空间按 90% 计）"""
    need = sum(int(f.size or 0) for f in files)
    others = [p for p in enabled_points() if p.id != point.id]
    available = sum(remaining_bytes(p) for p in others)
    return {
        "need": need,
        "available": available,
        "feasible": bool(others) and available >= need,
        "points": len(others),
    }


def transfer_object(src_point, dst_point, storage_key: str):
    """把单个文件实体从 src_point 搬到 dst_point"""
    local = read_path(src_point, storage_key)
    write_from_path(dst_point, storage_key, local, move=True)


# ---------------- 删除存储点 ----------------

# 已丢失记录在库中的保留天数，超期由后台任务清理
LOST_RETENTION_DAYS = 7


def storage_files(point):
    """存储点上的实体文件记录（不含目录与已丢失记录）"""
    return File.query.filter(
        File.storage_id == point.id,
        File.is_dir == False,  # noqa: E712
        File.is_lost == False,  # noqa: E712
    ).all()


def delete_plan(point) -> dict:
    """删除存储点前的评估：待处理文件数与转移可行性"""
    files = storage_files(point)
    plan = transfer_plan(point, files)
    plan["count"] = len(files)
    return plan


def migrate_point_data(point, progress=None) -> int:
    """把 point 上的文件实体均衡转移到其他存储点，返回转移的文件数

    先整体规划落点（空间不足时在搬运任何数据前即失败），再逐个搬运。
    """
    files = storage_files(point)
    allocator = Allocator(exclude_ids=[point.id])
    assignments = [(f, allocator.pick(int(f.size or 0))) for f in files]

    moved = 0
    total = len(assignments)
    for f, dst in assignments:
        try:
            transfer_object(point, dst, f.storage_key)
        except StorageError as e:
            raise StorageError(f"转移「{f.name}」失败：{e}")
        f.storage_id = dst.id
        db.session.commit()
        moved += 1
        if progress:
            progress(moved, total, f)
    return moved


def drop_point_data(point) -> int:
    """删除 point 上的全部文件实体，并把对应记录标记为已丢失"""
    files = storage_files(point)
    now = utcnow()
    for f in files:
        delete_object_quietly(point, f.storage_key)
        f.is_lost = True
        f.lost_at = now
        f.storage_id = None
    db.session.commit()
    return len(files)


def purge_lost_records(days: int = LOST_RETENTION_DAYS) -> int:
    """清理超过保留期的已丢失文件记录，返回删除的记录数"""
    cutoff = utcnow() - timedelta(days=int(days))
    nodes = File.query.filter(
        File.is_lost == True,  # noqa: E712
        File.lost_at.isnot(None),
        File.lost_at < cutoff,
    ).all()
    for node in nodes:
        db.session.delete(node)
    if nodes:
        db.session.commit()
    return len(nodes)


def test_connection(point) -> str:
    """校验存储点配置可用，返回错误信息；可用时返回空串"""
    if point.kind == "local":
        path = (point.path or "").strip()
        if not path:
            return "请填写本地存储目录"
        if not os.path.isdir(path):
            return "目录不存在或不可访问"
        if not os.access(path, os.W_OK):
            return "目录不可写"
        return ""
    if point.kind == "sftp":
        if not (point.host or "").strip():
            return "请填写 SFTP 服务器地址"
        try:
            client = _sftp_connect(point)
        except StorageError as e:
            return str(e)
        try:
            sftp = client.open_sftp()
            try:
                _sftp_makedirs(sftp, (point.remote_dir or "/").rstrip("/") or "/")
            finally:
                sftp.close()
            return ""
        except Exception as e:  # noqa: BLE001
            return f"SFTP 目录不可用：{e}"
        finally:
            _sftp_close(client)
    if point.kind == "s3":
        try:
            client = _s3_client(point)
        except StorageError as e:
            return str(e)
        try:
            client.head_bucket(Bucket=_s3_bucket(point))
            return ""
        except StorageError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"S3 连接失败：{e}"
    if not (point.host or "").strip():
        return "请填写 FTP 服务器地址"
    try:
        conn = _ftp_connect(point)
    except StorageError as e:
        return str(e)
    try:
        _ftp_makedirs(conn, (point.remote_dir or "/").rstrip("/"))
        return ""
    except FTP_ERRORS as e:
        return f"FTP 目录不可用：{e}"
    finally:
        _ftp_close(conn)


def clean_cache(ttl: int = None) -> int:
    """清理超过 TTL 的远端存储点本地缓存，返回删除文件数"""
    root = current_app.config["STORAGE_CACHE_ROOT"]
    if not root or not os.path.isdir(root):
        return 0
    ttl = int(ttl or current_app.config.get("STORAGE_CACHE_TTL") or 3600)
    cutoff = time.time() - ttl
    removed = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except OSError:
                continue
    return removed
