"""分享外链路由

安全设计：
- 分享内容只在网页内展示，下载一律走 POST /share/<token>/download，
  杜绝「扒出直链」直接用 URL 拉取文件（GET 不返回文件字节）。
- 提取密码为可选项；密码校验成功后给当前浏览器会话授权，
  后续浏览/下载无需重复输入；授权跟随带签名的会话 Cookie，无法伪造。
- 密码错误限次（按 IP+分享）防暴力破解，超限临时锁定。
"""
import time
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    send_file, abort, session, Response,
)
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db
from app.models import ShareLink, File

share_bp = Blueprint("share", __name__)

# ---- 密码暴力破解防护（进程内，按 IP+分享） ----
MAX_PWD_FAIL = 5
PWD_LOCK_SECONDS = 15 * 60
_pwd_guard = {}  # (token, ip) -> [fail_count, first_fail_ts]


def _lock_state(token: str):
    key = (token, request.remote_addr or "")
    now = datetime.utcnow().timestamp()
    rec = _pwd_guard.get(key)
    if not rec:
        return None, 0
    cnt, first = rec
    if cnt >= MAX_PWD_FAIL:
        remaining = PWD_LOCK_SECONDS - (now - first)
        if remaining > 0:
            return rec, int(remaining)
        _pwd_guard.pop(key, None)  # 冷却结束，清零
    return rec, 0


def _record_fail(token: str):
    key = (token, request.remote_addr or "")
    now = datetime.utcnow().timestamp()
    rec = _pwd_guard.get(key) or [0, now]
    rec[0] += 1
    rec[1] = rec[1] if rec[0] > 1 else now
    _pwd_guard[key] = rec


def _clear_fail(token: str):
    _pwd_guard.pop((token, request.remote_addr or ""), None)


# ---- 会话授权：密码验证通过后，同一浏览器免重复输入 ----
def _granted(token: str) -> bool:
    return bool(session.get("share_auth", {}).get(token))


def _grant(token: str):
    auth = dict(session.get("share_auth") or {})
    auth[token] = True
    session["share_auth"] = auth


def _load_share(token: str):
    """加载并校验分享状态，返回 (link, file)；无效则 abort"""
    link = ShareLink.query.filter_by(token=token).first()
    if link is None:
        abort(404, "分享不存在")
    if link.expire_at and link.expire_at < datetime.utcnow():
        abort(410, "分享已过期")
    if link.max_downloads is not None and link.download_count >= link.max_downloads:
        abort(410, "下载次数已用完")

    f = db.session.get(File, link.file_id)
    if f is None:
        abort(404, "文件不存在")
    # 被分享节点被移入回收站/删除时不可再访问
    if f.deleted_at is not None:
        abort(410, "文件已被删除")
    return link, f


@share_bp.route("/shares")
@login_required
def my_shares():
    links = ShareLink.query.filter_by(user_id=current_user.id).order_by(
        ShareLink.created_at.desc()).all()
    return render_template("share/list.html", links=links)


@share_bp.route("/share/create", methods=["POST"])
@login_required
def create():
    file_id = request.form.get("file_id")
    try:
        f = db.session.get(File, int(file_id))
    except (TypeError, ValueError):
        abort(404, "文件不存在或无权访问")
    if f is None or f.user_id != current_user.id:
        abort(403)

    password = request.form.get("password") or None
    expire_days = request.form.get("expire_days") or None
    max_downloads = request.form.get("max_downloads") or None

    link = ShareLink(file_id=f.id, user_id=current_user.id)
    if password:
        link.password_hash = generate_password_hash(password)
    if expire_days:
        try:
            days = int(expire_days)
        except (TypeError, ValueError):
            abort(400, "有效期必须是整数天")
        link.expire_at = datetime.utcnow() + timedelta(days=days)
    if max_downloads:
        try:
            link.max_downloads = int(max_downloads)
        except (TypeError, ValueError):
            abort(400, "下载次数必须是整数")

    db.session.add(link)
    db.session.commit()
    flash("分享链接已生成", "success")
    return redirect(url_for("share.my_shares"))


@share_bp.route("/share/<token>", methods=["GET", "POST"])
def access(token):
    """分享入口页：密码校验 + 展示落地页/目录浏览页（GET 永不直接返回文件流）"""
    link, f = _load_share(token)

    # ---- 密码（可选）校验 ----
    if link.password_hash and not _granted(token):
        lock, remain = _lock_state(token)
        if lock and remain:
            flash(f"密码错误次数过多，请 {remain // 60 + 1} 分钟后再试", "danger")
            return render_template("share/password.html", token=token)
        if request.method == "POST":
            pw = request.form.get("password") or ""
            if check_password_hash(link.password_hash, pw):
                _clear_fail(token)
                _grant(token)
                return redirect(url_for("share.access", token=token))
            _record_fail(token)
            flash("密码错误", "danger")
        return render_template("share/password.html", token=token)

    return _show_page(link, f)


def _show_page(link: ShareLink, f: File):
    """落地页：文件 → 下载页；文件夹 → 目录浏览页"""
    from app.services import file_service

    if f.is_dir:
        folder = f
        folder_id = request.args.get("folder_id")
        if folder_id:
            try:
                folder = db.session.get(File, int(folder_id))
            except (TypeError, ValueError):
                abort(404, "目录不存在")
            if folder is None or not folder.is_dir or not _node_in_tree(folder, f):
                abort(404, "目录不存在")
        items = file_service.active_children(folder)
        crumbs = _build_crumbs(folder, f)
        return render_template("share/browse.html", link=link, root=f,
                               folder=folder, items=items, crumbs=crumbs)
    return render_template("share/landing.html", link=link, f=f)


@share_bp.route("/share/<token>/download", methods=["POST"])
def download(token):
    """下载端点（仅 POST）：网页内点下载按钮触发，不能靠 GET 直链拉取"""
    link, f = _load_share(token)

    # 密码分享必须已通过本浏览器验证
    if link.password_hash and not _granted(token):
        return redirect(url_for("share.access", token=token))

    # 可选：文件夹分享内下载指定子文件（POST body: file=<id>）
    target = f
    file_id = request.form.get("file") or request.args.get("file")
    if file_id:
        try:
            node = db.session.get(File, int(file_id))
        except (TypeError, ValueError):
            abort(404, "文件不存在或无权访问")
        if node is None or node.is_dir or not _node_in_tree(node, f):
            abort(404, "文件不存在或无权访问")
        target = node
    elif f.is_dir:
        abort(400, "请进入目录后选择要下载的文件")

    return _send_file(link, target)


def _node_in_tree(node: File, root: File) -> bool:
    """node 是否位于 root 的子树内（含自身），沿途节点均未删除"""
    cur = node
    while cur is not None:
        if cur.id == root.id:
            return True
        if cur.deleted_at is not None or cur.user_id != root.user_id:
            return False
        cur = cur.parent
    return False


def _build_crumbs(folder: File, root: File):
    """分享目录内的面包屑（root → folder）"""
    crumbs = []
    cur = folder
    seen = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        crumbs.append(cur)
        if cur.id == root.id:
            break
        cur = cur.parent
    crumbs.reverse()
    return crumbs


ANON_SPEED_LIMIT = 1 * 1024 * 1024  # 未登录访客下载限速 1 MiB/s


def _send_file(link: ShareLink, f: File):
    import os
    from app.services import file_service
    path = file_service.get_physical_path(f.storage_key)
    if not os.path.exists(path):
        abort(404, "文件实体丢失")
    link.download_count += 1
    db.session.commit()
    if current_user.is_authenticated:
        return send_file(path, as_attachment=True, download_name=f.name)
    return _stream_limited(path, f.name, f.size)


def _stream_limited(path: str, download_name: str, size: int):
    """未登录访客下载：按 1 MiB/s 流式限速"""
    def generate():
        chunk = 256 * 1024
        with open(path, "rb") as fh:
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                yield data
                time.sleep(len(data) / ANON_SPEED_LIMIT)

    from urllib.parse import quote
    resp = Response(generate(), direct_passthrough=True)
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(download_name)}"
    resp.headers["Content-Length"] = str(size)
    return resp


@share_bp.route("/share/<int:link_id>/cancel", methods=["POST"])
@login_required
def cancel(link_id):
    link = ShareLink.query.filter_by(id=link_id, user_id=current_user.id).first()
    if link:
        db.session.delete(link)
        db.session.commit()
        flash("已取消分享", "success")
    return redirect(url_for("share.my_shares"))
