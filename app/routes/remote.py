"""远程下载路由：创建任务、任务列表、取消"""
from urllib.parse import urlsplit, unquote

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, jsonify,
)
from flask_login import login_required, current_user
from werkzeug.exceptions import abort

from app.extensions import db
from app.models import File, RemoteDownload
from app.services import file_service
from app.services import remote_service
from app.utils.helpers import safe_filename, utcnow

remote_bp = Blueprint("remote", __name__, url_prefix="/remote")


@remote_bp.route("")
@login_required
def index():
    tasks = RemoteDownload.query.filter_by(user_id=current_user.id).order_by(
        RemoteDownload.created_at.desc()).all()
    limit = remote_service.concurrency_limit(current_user)
    running = RemoteDownload.query.filter_by(
        user_id=current_user.id, status="downloading").count()
    return render_template("remote/index.html", tasks=tasks,
                           limit=limit, running=running)


@remote_bp.route("/create", methods=["POST"])
@login_required
def create():
    url = (request.form.get("url") or "").strip()
    parent_id = request.form.get("parent_id") or None
    filename = (request.form.get("filename") or "").strip()

    if not url:
        flash("请输入下载地址", "danger")
        return redirect(request.referrer or url_for("remote.index"))
    try:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError
    except ValueError:
        flash("仅支持 http/https 下载地址", "danger")
        return redirect(request.referrer or url_for("remote.index"))
    if len(url) > 2048:
        flash("下载地址过长", "danger")
        return redirect(request.referrer or url_for("remote.index"))

    # 目标目录校验（可选）
    parent = None
    if parent_id:
        parent = file_service.get_dir_by_id(current_user, parent_id)
        if parent is None:
            flash("目标文件夹不存在或无权访问", "danger")
            return redirect(request.referrer or url_for("remote.index"))

    if not filename:
        path = unquote(urlsplit(url).path)
        base = path.rsplit("/", 1)[-1] if "/" in path else ""
        filename = safe_filename(base) or "download"
    else:
        filename = safe_filename(filename)

    task = RemoteDownload(
        user_id=current_user.id,
        parent_id=parent.id if parent else None,
        url=url,
        filename=filename,
    )
    db.session.add(task)
    db.session.commit()
    flash("远程下载任务已创建，排队自动开始", "success")
    return redirect(request.referrer or url_for("remote.index"))


@remote_bp.route("/<int:task_id>/cancel", methods=["POST"])
@login_required
def cancel(task_id):
    task = RemoteDownload.query.filter_by(id=task_id, user_id=current_user.id).first()
    if task is None:
        abort(404, "任务不存在")
    if task.status in ("queued", "downloading"):
        task.status = "canceled"
        task.finished_at = utcnow()
        db.session.commit()
        flash("任务已取消", "success")
    else:
        flash("该任务已结束，无法取消", "warning")
    return redirect(url_for("remote.index"))


@remote_bp.route("/<int:task_id>/delete", methods=["POST"])
@login_required
def delete(task_id):
    task = RemoteDownload.query.filter_by(id=task_id, user_id=current_user.id).first()
    if task is None:
        abort(404, "任务不存在")
    if task.status in ("queued", "downloading"):
        flash("任务执行中，请先取消", "warning")
        return redirect(url_for("remote.index"))
    db.session.delete(task)
    db.session.commit()
    flash("任务记录已删除", "success")
    return redirect(url_for("remote.index"))
