"""文件核心业务逻辑"""
import os
import shutil
from datetime import datetime

from flask import current_app

from app.extensions import db
from app.models import File, User
from app.services.quota_service import QuotaError, effective_quota
from app.utils.helpers import safe_filename, gen_storage_key


def _storage_path(storage_key: str) -> str:
    root = current_app.config["STORAGE_ROOT"]
    # 用前两位做子目录分片，避免单目录文件过多
    sub = storage_key[:2]
    d = os.path.join(root, sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, storage_key)


def get_dir_by_id(user: User, parent_id):
    """安全解析目标目录：过滤非法值、越权目录与回收站中的目录"""
    if parent_id in (None, 0, "", "0"):
        return None
    try:
        pid = int(parent_id)
    except (TypeError, ValueError):
        return None
    d = db.session.get(File, pid)
    if d is None or d.user_id != user.id or not d.is_dir or d.deleted_at is not None:
        return None
    return d


def list_dir(user: User, parent_id, sort="name", order="asc"):
    parent = get_dir_by_id(user, parent_id)
    q = File.query.filter_by(user_id=user.id, parent_id=parent_id)
    # 隐藏回收站中的文件
    q = q.filter(File.deleted_at.is_(None))
    items = _ordered(q, sort, order).all()
    return parent, items


_SORT_COLUMNS = {"name": File.name, "size": File.size, "time": File.updated_at}
_ALLOWED_SORTS = set(_SORT_COLUMNS)
_ALLOWED_ORDERS = {"asc", "desc"}


def _ordered(query, sort="name", order="asc"):
    """统一排序：目录恒排前，再按 name/size/time 及方向排序"""
    col = _SORT_COLUMNS.get(sort if sort in _ALLOWED_SORTS else "name", File.name)
    expr = col.desc() if order == "desc" else col.asc()
    return query.order_by(File.is_dir.desc(), expr)


def search(user: User, keyword: str, sort="name", order="asc"):
    """全局按文件名搜索当前用户的未删除文件/文件夹"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    q = File.query.filter(
        File.user_id == user.id,
        File.deleted_at.is_(None),
        File.name.ilike(f"%{kw}%"),
    )
    return _ordered(q, sort, order).all()


def get_breadcrumb(parent: File):
    """生成面包屑导航"""
    crumbs = []
    cur = parent
    while cur is not None:
        crumbs.append(cur)
        cur = cur.parent
    crumbs.reverse()
    return crumbs


def create_folder(user: User, parent_id, name):
    name = safe_filename(name)
    parent = get_dir_by_id(user, parent_id)
    if _sibling_conflict(user, parent, name):
        raise ValueError("同级已存在同名文件或文件夹")
    f = File(user_id=user.id, parent_id=parent.id if parent else None,
             name=name, is_dir=True)
    db.session.add(f)
    db.session.commit()
    return f


def _sibling_conflict(user, parent, name, exclude_id=None) -> bool:
    """同级（或根目录）是否存在同名节点"""
    pid = parent.id if parent else None
    q = File.query.filter(
        File.user_id == user.id,
        File.parent_id == pid,
        File.name == name,
        File.deleted_at.is_(None),
    )
    if exclude_id is not None:
        q = q.filter(File.id != exclude_id)
    return q.first() is not None


def save_uploaded_file(user: User, parent_id, filename, size, file_obj, md5=None, mime=None):
    """保存一个已校验的上传文件，返回 File"""
    filename = safe_filename(filename)
    parent = get_dir_by_id(user, parent_id)
    ext = os.path.splitext(filename)[1]
    storage_key = gen_storage_key(ext)

    dest = _storage_path(storage_key)
    file_obj.save(dest)

    f = File(
        user_id=user.id,
        parent_id=parent.id if parent else None,
        name=filename,
        is_dir=False,
        size=size,
        mime=mime,
        storage_key=storage_key,
        md5=md5,
    )
    db.session.add(f)
    db.session.commit()
    return f


def save_chunked_file(user: User, parent_id, filename, total_size, tmp_path, md5=None, mime=None):
    """分片合并完成后，从临时文件移动到正式存储"""
    filename = safe_filename(filename)
    parent = get_dir_by_id(user, parent_id)
    ext = os.path.splitext(filename)[1]
    storage_key = gen_storage_key(ext)
    dest = _storage_path(storage_key)
    shutil.move(tmp_path, dest)

    f = File(
        user_id=user.id,
        parent_id=parent.id if parent else None,
        name=filename,
        is_dir=False,
        size=total_size,
        mime=mime,
        storage_key=storage_key,
        md5=md5,
    )
    db.session.add(f)
    db.session.commit()
    return f


def create_reference(user: User, parent_id, filename, size, mime, md5, storage_key):
    """秒传：复用已有物理文件，在目标位置创建一条引用记录"""
    filename = safe_filename(filename)
    parent = get_dir_by_id(user, parent_id)
    f = File(
        user_id=user.id,
        parent_id=parent.id if parent else None,
        name=filename,
        is_dir=False,
        size=size,
        mime=mime,
        storage_key=storage_key,
        md5=md5,
    )
    db.session.add(f)
    db.session.commit()
    return f


def rename(user: User, file_id, new_name):
    f = _get_owned_file(user, file_id)
    new_name = safe_filename(new_name)
    if new_name == f.name:
        return f
    if _sibling_conflict(user, f.parent if f.parent_id else None, new_name, exclude_id=f.id):
        raise ValueError("同级已存在同名文件或文件夹")
    f.name = new_name
    db.session.commit()
    return f


def _ensure_not_inside(node: File, target: File):
    """校验 target 不在 node 自身或其子孙目录内（防止移动/复制成环）"""
    if node.is_dir and target is not None:
        cur = target
        while cur is not None:
            if cur.id == node.id:
                raise ValueError("不能移动/复制到自身或其子文件夹中")
            cur = cur.parent


def move(user: User, file_id, target_parent_id):
    f = _get_owned_file(user, file_id)
    target = get_dir_by_id(user, target_parent_id)
    target_pid = target.id if target else None
    if target_pid == f.parent_id:
        return f
    _ensure_not_inside(f, target)
    if _sibling_conflict(user, target, f.name, exclude_id=f.id):
        raise ValueError("目标位置已存在同名文件或文件夹")
    f.parent_id = target_pid
    db.session.commit()
    return f


def copy(user: User, file_id, target_parent_id):
    """复制文件/文件夹（递归），复制前校验容量与目录环"""
    f = _get_owned_file(user, file_id)
    target = get_dir_by_id(user, target_parent_id)
    _ensure_not_inside(f, target)

    # 容量校验：复制后不能超出剩余空间
    total = tree_size(user, f)
    free = effective_quota(user)["free_storage"]
    if total > free:
        from app.utils.helpers import human_size
        raise QuotaError(f"剩余空间不足，本次复制需 {human_size(total)}，剩余 {human_size(free)}")

    new_name = _unique_copy_name(user, target, f.name)
    new_f = _copy_node(user, f, target, new_name)
    db.session.commit()
    return new_f


def tree_size(user: User, node: File) -> int:
    """目录树内未删除文件的总体积（复制/打包体积用）"""
    total = node.size if not node.is_dir else 0
    if node.is_dir:
        for child in File.query.filter_by(user_id=user.id, parent_id=node.id).filter(
                File.deleted_at.is_(None)).all():
            total += tree_size(user, child)
    return total


def _unique_copy_name(user: User, target, name: str) -> str:
    pid = target.id if target else None
    stem, ext = os.path.splitext(name)
    candidate = name
    n = 1
    while _exists_name(user, pid, candidate):
        candidate = f"{stem} 副本{'' if n == 1 else f' ({n})'}{ext}"
        n += 1
    return candidate


def _exists_name(user: User, parent_id, name: str) -> bool:
    return File.query.filter(
        File.user_id == user.id,
        File.parent_id == parent_id,
        File.name == name,
        File.deleted_at.is_(None),
    ).first() is not None


def _copy_node(user: User, src: File, target_parent: File, name: str):
    if src.is_dir:
        new_dir = File(user_id=user.id,
                       parent_id=target_parent.id if target_parent else None,
                       name=name, is_dir=True)
        db.session.add(new_dir)
        db.session.flush()
        for child in File.query.filter_by(user_id=user.id, parent_id=src.id).filter(
                File.deleted_at.is_(None)).all():
            _copy_node(user, child, new_dir, child.name)
        return new_dir
    else:
        # 复制物理文件
        src_path = _storage_path(src.storage_key)
        if not os.path.exists(src_path):
            raise ValueError(f"文件 {src.name} 物理实体丢失，无法复制")
        ext = os.path.splitext(name)[1]
        storage_key = gen_storage_key(ext)
        shutil.copy2(src_path, _storage_path(storage_key))
        new_f = File(user_id=user.id,
                     parent_id=target_parent.id if target_parent else None,
                     name=name, is_dir=False, size=src.size, mime=src.mime,
                     storage_key=storage_key, md5=src.md5)
        db.session.add(new_f)
        return new_f


def soft_delete(user: User, file_id):
    """删除到回收站"""
    f = _get_owned_file(user, file_id)
    _soft_delete_node(user, f)


def _soft_delete_node(user: User, node: File):
    if node.is_dir:
        for child in File.query.filter_by(user_id=user.id, parent_id=node.id).filter(
                File.deleted_at.is_(None)).all():
            _soft_delete_node(user, child)
    node.deleted_at = datetime.utcnow()
    node.deleted_original_parent_id = node.parent_id
    db.session.add(node)


def hard_delete(user: User, file_id):
    """彻底删除（物理清除）"""
    f = _get_owned_file(user, file_id)
    _hard_delete_node(user, f)
    db.session.commit()


def _hard_delete_node(user: User, node: File):
    if node.is_dir:
        for child in File.query.filter_by(user_id=user.id, parent_id=node.id).all():
            _hard_delete_node(user, child)
    if not node.is_dir and node.storage_key:
        # 秒传引用共享同一物理文件，仅当无其他记录引用时才删除实体
        refs = File.query.filter(
            File.storage_key == node.storage_key,
            File.id != node.id,
        ).count()
        if refs == 0:
            p = _storage_path(node.storage_key)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    db.session.delete(node)


def restore(user: User, file_id):
    """从回收站恢复"""
    f = _get_owned_file(user, file_id)
    if f.deleted_at is None:
        raise ValueError("该文件不在回收站")
    _restore_node(user, f, is_root=True)
    db.session.commit()


def _restore_node(user: User, node: File, is_root: bool = False):
    if node.is_dir:
        for child in File.query.filter_by(user_id=user.id, parent_id=node.id).all():
            _restore_node(user, child, is_root=False)
    orig = node.deleted_original_parent_id
    if is_root and orig is not None:
        # 原父目录已不存在/被删除/越权时，恢复到根目录
        op = db.session.get(File, orig)
        if op is None or op.user_id != user.id or op.deleted_at is not None or not op.is_dir:
            orig = None
    node.deleted_at = None
    node.deleted_original_parent_id = None
    node.parent_id = orig
    db.session.add(node)


def list_trash(user: User):
    """回收站仅显示顶层被删节点（其子树由级联删除一并处理）"""
    nodes = File.query.filter_by(user_id=user.id).filter(
        File.deleted_at.is_not(None)
    ).all()
    visible = []
    for node in nodes:
        if node.parent_id is None:
            visible.append(node)
            continue
        parent = db.session.get(File, node.parent_id)
        if parent is None or parent.user_id != user.id or parent.deleted_at is None:
            visible.append(node)
    visible.sort(key=lambda n: n.deleted_at or datetime.min, reverse=True)
    return visible


def empty_trash(user: User) -> int:
    """清空回收站：仅处理顶层被删节点（子树由其递归硬删）"""
    nodes = list_trash(user)
    for node in nodes:
        _hard_delete_node(user, node)
    db.session.commit()
    return len(nodes)


def active_children(node: File):
    """节点的未删除子节点（打包/目录遍历用）"""
    return File.query.filter_by(user_id=node.user_id, parent_id=node.id).filter(
        File.deleted_at.is_(None)).all()


def purge_user_data(user: User):
    """定时任务：硬删除用户全部文件（含引用计数处理）"""
    nodes = File.query.filter_by(user_id=user.id).all()
    owned_ids = {n.id for n in nodes}
    # 只遍历顶层节点，目录内部由 _hard_delete_node 递归处理，避免重复删除
    for node in nodes:
        if node.parent_id is None or node.parent_id not in owned_ids:
            _hard_delete_node(user, node)
    db.session.commit()


def _get_owned_file(user: User, file_id) -> File:
    f = db.session.get(File, int(file_id))
    if f is None or f.user_id != user.id:
        raise PermissionError("文件不存在或无权访问")
    return f


def get_owned_file(user: User, file_id) -> File:
    return _get_owned_file(user, file_id)


def get_physical_path(storage_key: str) -> str:
    return _storage_path(storage_key)