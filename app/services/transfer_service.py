"""远端存储点传输任务（取回 / 上传）的后台执行服务

远端读写放在请求线程里会撞上 gunicorn 的超时（大文件直接 502），因此统一改为
「入队 -> 后台线程执行 -> 前端按 token 轮询进度」：

- fetch：远端 -> 本地缓存，完成后请求方直接读缓存
- store：本地暂存 -> 远端，完成后删除暂存文件

任务行以 (kind, point_id, storage_key) 唯一，天然完成并发去重：同一文件被多人
同时取回时只会真正传输一次，其余请求共用同一 token 的进度。
"""
import os
import shutil
import tempfile
import threading
import time
from datetime import timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import File, TransferTask
from app.services import storage_service
from app.utils.helpers import utcnow

POLL_SECONDS = 2             # 轮询间隔
MAX_PROCESS_ACTIVE = 4       # 单进程并行传输上限；跨进程由「乐观认领」控制
SWEEP_IDLE_SECONDS = 600     # running 任务超过该时间无进度即判定执行者失联
TASK_MAX_SECONDS = 7200      # 单任务最长执行 2 小时
PROGRESS_MIN_INTERVAL = 0.5  # 进度写库最小间隔（秒）

_started = False
_started_lock = threading.Lock()
_active_ids = set()
_active_lock = threading.Lock()


# --------------------------- 队列 ---------------------------

def enqueue_fetch(point, storage_key: str) -> TransferTask:
    """把「远端 -> 本地缓存」任务放入队列（同文件已有在途任务则复用）"""
    return _enqueue("fetch", point.id, storage_key)


def enqueue_store(point, storage_key: str, src_path: str) -> TransferTask:
    """把「本地暂存 -> 远端」任务放入队列"""
    return _enqueue("store", point.id, storage_key, src_path)


def stage_stream(stream) -> str:
    """把上传流落到传输暂存文件，返回路径（远端写入前的中转）

    暂存在独立目录，不会与分片上传会话的清理逻辑相互影响。
    """
    root = os.path.join(current_app.config["UPLOAD_TMP_ROOT"], "transfer")
    os.makedirs(root, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=root, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        _remove_quietly(path)
        raise
    return path


def stage_path(src_path: str) -> str:
    """把已落盘的临时文件移到传输暂存目录（避免被调用方的清理逻辑删掉）"""
    root = os.path.join(current_app.config["UPLOAD_TMP_ROOT"], "transfer")
    os.makedirs(root, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=root, suffix=".part")
    os.close(fd)
    try:
        os.replace(src_path, path)
    except OSError:
        # 临时目录与暂存目录不在同一文件系统时退化为复制
        shutil.move(src_path, path)
    return path


def ensure_local(point, storage_key: str):
    """取回文件供本次请求直接读取

    返回 (path, task)：path 非空表示已可读（本地存储点、缓存命中或正在上传的
    暂存文件），path 为空且 task 非空表示已入队取回，调用方应展示进度页轮询。
    """
    if point is None:
        return None, None
    path = storage_service.cached_path(point, storage_key)
    if path:
        return path, None
    staged = staged_path(point, storage_key)
    if staged:
        return staged, None  # 正在上传到远端：暂存文件本身就可读，无需等待
    if not storage_service.is_remote(point):
        return None, None  # 本地存储点且实体不存在，交由调用方按文件丢失处理
    return None, enqueue_fetch(point, storage_key)


def staged_path(point, storage_key: str):
    """正在上传中的文件读取本地暂存路径（供边上传边预览/下载）"""
    if point is None:
        return None
    task = TransferTask.query.filter_by(
        kind="store", point_id=point.id, storage_key=storage_key).filter(
        TransferTask.status.in_(("queued", "running"))).first()
    if task is None or not task.src_path or not os.path.exists(task.src_path):
        return None
    return task.src_path


def _enqueue(kind: str, point_id: int, storage_key: str,
             src_path: str = None) -> TransferTask:
    task = TransferTask.query.filter_by(
        kind=kind, point_id=point_id, storage_key=storage_key).first()
    if task is not None and task.status in ("queued", "running"):
        return task

    if task is None:
        task = TransferTask(kind=kind, point_id=point_id, storage_key=storage_key)
        db.session.add(task)
    # 唯一约束下同一文件只允许一行，已完成的任务复用该行重新入队
    task.src_path = src_path
    task.status = "queued"
    task.total_bytes = 0
    task.done_bytes = 0
    task.error = None
    task.created_at = utcnow()
    task.started_at = None
    task.last_progress_at = None
    task.finished_at = None
    try:
        db.session.commit()
    except IntegrityError:
        # 并发进程恰好同时入队，回查已在途的那条
        db.session.rollback()
        task = TransferTask.query.filter_by(
            kind=kind, point_id=point_id, storage_key=storage_key).first()
    return task


# --------------------------- 状态查询 ---------------------------

def task_status(token: str):
    """单个任务的进度（token 不存在时返回 None）"""
    if not token:
        return None
    task = TransferTask.query.filter_by(token=token).first()
    return _status_of(task) if task is not None else None


def aggregate_status(tokens) -> dict:
    """多个任务聚合进度（批量下载：全部就绪才继续）"""
    tasks = []
    for token in tokens:
        task = TransferTask.query.filter_by(token=token).first() if token else None
        if task is not None:
            tasks.append(task)
    if not tasks:
        return {"status": "failed", "error": "任务不存在", "done": 0,
                "total": 0, "percent": 0, "count": 0}

    done = sum(int(t.done_bytes or 0) for t in tasks)
    total = sum(int(t.total_bytes or 0) for t in tasks)
    failed = next((t for t in tasks if t.status == "failed"), None)
    if failed is not None:
        status = "failed"
    elif all(t.status == "done" for t in tasks):
        status = "done"
    else:
        status = "running"
    if status == "done":
        percent = 100
    elif total > 0:
        percent = min(99, int(done * 100 / total))
    else:
        percent = 0
    return {
        "status": status,
        "done": done,
        "total": total,
        "percent": percent,
        "error": (failed.error or "传输失败") if failed is not None else "",
        "count": len(tasks),
    }


def _status_of(task) -> dict:
    done = int(task.done_bytes or 0)
    total = int(task.total_bytes or 0)
    if task.status == "done":
        percent = 100
    elif total > 0:
        percent = min(99, int(done * 100 / total))
    else:
        percent = 0
    return {
        "token": task.token,
        "kind": task.kind,
        "status": task.status,
        "done": done,
        "total": total,
        "percent": percent,
        "error": task.error or "",
    }


def pending_map() -> dict:
    """在途任务映射 {(存储点 id, storage_key): task}，供列表标记「取回中/上传中」"""
    try:
        tasks = TransferTask.query.filter(
            TransferTask.status.in_(("queued", "running"))).all()
    except Exception:  # noqa: BLE001 —— 列表标记失败不影响主流程
        return {}
    return {(int(t.point_id), t.storage_key): t for t in tasks}


# --------------------------- Worker ---------------------------

def start_worker(app):
    """在进程内启动一次传输轮询线程（幂等）"""
    global _started
    with _started_lock:
        if _started:
            return None
        _started = True

    def _loop():
        while True:
            try:
                with app.app_context():
                    _sweep_stale_tasks()
                    _dispatch(app)
            except Exception:  # noqa: BLE001
                db.session.rollback()
            time.sleep(POLL_SECONDS)

    t = threading.Thread(target=_loop, name="transfer-worker", daemon=True)
    t.start()
    return t


def _sweep_stale_tasks():
    """回收执行者失联（长时间无进度）或超时的在途任务"""
    now = utcnow()
    idle = now - timedelta(seconds=SWEEP_IDLE_SECONDS)
    overtime = now - timedelta(seconds=TASK_MAX_SECONDS)
    stale = TransferTask.query.filter_by(status="running").filter(
        (TransferTask.last_progress_at.is_(None)) | (TransferTask.last_progress_at < idle)
    ).all()
    for task in stale:
        _fail(task, "传输进程中断，任务已重置", drop_src=True)
    long = TransferTask.query.filter_by(status="running").filter(
        TransferTask.started_at < overtime).all()
    for task in long:
        _fail(task, "传输超时", drop_src=True)
    if stale or long:
        db.session.commit()


def _dispatch(app):
    """扫描排队任务并认领，随后分派到独立线程执行"""
    queued = TransferTask.query.filter_by(status="queued").order_by(
        TransferTask.created_at.asc()).all()
    for task in queued:
        with _active_lock:
            if len(_active_ids) >= MAX_PROCESS_ACTIVE:
                return
        # 乐观认领：仅当仍是 queued 时才置为 running，避免多进程重复执行
        claimed = TransferTask.query.filter_by(id=task.id, status="queued").update({
            "status": "running",
            "started_at": utcnow(),
            "last_progress_at": utcnow(),
        })
        db.session.commit()
        if claimed != 1:
            continue
        with _active_lock:
            _active_ids.add(task.id)
        threading.Thread(target=_runner, args=(app, task.id), daemon=True).start()


def _runner(app, task_id: int):
    try:
        with app.app_context():
            _run_task(task_id)
    finally:
        with _active_lock:
            _active_ids.discard(task_id)


# --------------------------- 单任务执行 ---------------------------

def _run_task(task_id: int):
    task = db.session.get(TransferTask, task_id)
    if task is None or task.status != "running":
        return
    point = storage_service.get_point(task.point_id)
    if point is None:
        _fail(task, "存储点不存在", drop_src=True)
        db.session.commit()
        return

    tick = _ProgressTicker(task_id)
    size = 0
    try:
        if task.kind == "store":
            src = task.src_path
            if not src or not os.path.exists(src):
                raise ValueError("暂存文件已丢失，无法完成上传")
            size = os.path.getsize(src)
            storage_service.write_from_path(
                point, task.storage_key, src, move=True, on_progress=tick)
        else:
            path = storage_service.read_path(
                point, task.storage_key, on_progress=tick)
            size = os.path.getsize(path)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        task = db.session.get(TransferTask, task_id)
        if task is not None and task.status == "running":
            _fail(task, str(e)[:480], drop_src=True)
            db.session.commit()
        return

    tick.flush()
    _finish(task_id, size)


def _finish(task_id: int, size: int):
    task = db.session.get(TransferTask, task_id)
    if task is None or task.status != "running":
        return
    task.done_bytes = int(size or task.done_bytes or 0)
    task.total_bytes = int(size or task.total_bytes or 0)
    task.status = "done"
    task.error = None
    task.finished_at = utcnow()
    db.session.commit()


def _fail(task, message: str, drop_src: bool = False):
    task.status = "failed"
    task.error = (str(message or "").strip() or "传输失败")[:500]
    task.finished_at = utcnow()
    if drop_src and task.kind == "store" and task.src_path:
        _remove_quietly(task.src_path)


def _remove_quietly(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


class _ProgressTicker:
    """进度节流器：把底层的字节回调按最小间隔写库，避免每块一次 UPDATE"""

    def __init__(self, task_id: int):
        self._task_id = task_id
        self._last = 0.0
        self._latest = None

    def __call__(self, done: int, total: int = 0):
        self._latest = (int(done or 0), int(total or 0))
        now = time.monotonic()
        if now - self._last < PROGRESS_MIN_INTERVAL:
            return
        self._last = now
        self._write(*self._latest)

    def flush(self):
        if self._latest is not None:
            self._write(*self._latest)
            self._latest = None

    def _write(self, done: int, total: int):
        values = {"done_bytes": done, "last_progress_at": utcnow()}
        if total:
            values["total_bytes"] = total
        try:
            TransferTask.query.filter_by(
                id=self._task_id, status="running").update(values)
            db.session.commit()
        except Exception:  # noqa: BLE001 —— 进度写库失败不影响传输本身
            db.session.rollback()
