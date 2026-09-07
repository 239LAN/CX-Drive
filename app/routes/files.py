"""文件路由：浏览/上传/下载/增删改/分片/秒传/批量操作"""
import os
import time

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    send_file, jsonify, current_app, abort, Response, after_this_request,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models import File, UploadSession
from app.services import file_service
from app.services.quota_service import (
    QuotaError, check_upload, check_download, get_speed_limit,
    consume_traffic, effective_quota,
)
from app.utils.helpers import utcnow

files_bp = Blueprint("files", __name__)

_SORT_CHOICES = ("name", "size", "time")
_ORDER_CHOICES = ("asc", "desc")


def _parse_ids(raw) -> list:
    out = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


@files_bp.route("/files")
@files_bp.route("/files/<int:parent_id>")
@login_required
def index(parent_id=None):
    sort = request.args.get("sort") if request.args.get("sort") in _SORT_CHOICES else "name"
    order = request.args.get("order") if request.args.get("order") in _ORDER_CHOICES else "asc"
    keyword = (request.args.get("q") or "").strip()
    q = effective_quota(current_user)

    if keyword:
        items = file_service.search(current_user, keyword, sort, order)
        return render_template("files/index.html", parent=None, items=items, crumbs=[],
                               q=q, search=keyword, sort=sort, order=order)

    parent, items = file_service.list_dir(current_user, parent_id, sort, order)
    crumbs = file_service.get_breadcrumb(parent)
    return render_template("files/index.html", parent=parent, items=items, crumbs=crumbs,
                           q=q, search="", sort=sort, order=order)


@files_bp.route("/files/folder/new", methods=["POST"])
@login_required
def create_folder():
    parent_id = request.form.get("parent_id") or None
    name = request.form.get("name") or ""
    try:
        file_service.create_folder(current_user, parent_id, name)
        flash("文件夹创建成功", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/upload", methods=["POST"])
@login_required
def upload():
    """普通（小文件）上传"""
    parent_id = request.form.get("parent_id") or None
    f = request.files.get("file")
    if f is None:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))

    # 读取文件大小用于容量校验
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)

    try:
        check_upload(current_user, size)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    try:
        file_service.save_uploaded_file(
            current_user, parent_id, f.filename, size, f, mime=f.mimetype
        )
        consume_traffic(current_user, size, "upload")
        db.session.commit()
        flash("上传成功", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"上传失败：{e}", "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/download")
@login_required
def download(file_id):
    f = file_service.get_owned_file(current_user, file_id)
    if f.is_dir:
        return _download_folder(f)
    return _download_file(f)


def _download_file(f: File):
    try:
        check_download(current_user, f.size)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    path = file_service.get_physical_path(f.storage_key)
    if not os.path.exists(path):
        abort(404, "文件实体丢失")

    consume_traffic(current_user, f.size, "download")
    db.session.commit()

    limit = get_speed_limit(current_user)  # None = 不限
    return _stream_file(path, f.name, f.size, limit)


def _stream_file(path, download_name, size, speed_limit=None):
    """带限速的文件流式响应"""
    def generate():
        chunk = 256 * 1024
        with open(path, "rb") as fh:
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                yield data
                if speed_limit:
                    time.sleep(len(data) / speed_limit)

    resp = Response(generate(), direct_passthrough=True)
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{_urlquote(download_name)}"
    resp.headers["Content-Length"] = str(size)
    return resp


def _urlquote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


def _download_folder(f: File):
    """文件夹打包 zip 下载：先写临时文件再回传"""
    import zipfile
    import tempfile

    total = file_service.tree_size(current_user, f)
    try:
        check_download(current_user, total)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            _add_dir_to_zip(zf, f, "")
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise

    consume_traffic(current_user, total, "download")
    db.session.commit()

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return resp

    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name=f"{f.name}.zip")


def _add_dir_to_zip(zf, node: File, prefix: str):
    for child in file_service.active_children(node):
        arc = prefix + child.name
        if child.is_dir:
            _add_dir_to_zip(zf, child, arc + "/")
        else:
            p = file_service.get_physical_path(child.storage_key)
            if os.path.exists(p):
                zf.write(p, arc)


@files_bp.route("/files/<int:file_id>/rename", methods=["POST"])
@login_required
def rename(file_id):
    name = request.form.get("name") or ""
    try:
        file_service.rename(current_user, file_id, name)
        flash("重命名成功", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/move", methods=["POST"])
@login_required
def move(file_id):
    target = request.form.get("target_parent_id") or None
    try:
        file_service.move(current_user, file_id, target)
        flash("移动成功", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/copy", methods=["POST"])
@login_required
def copy(file_id):
    target = request.form.get("target_parent_id") or None
    try:
        file_service.copy(current_user, file_id, target)
        flash("复制成功", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(file_id):
    try:
        file_service.soft_delete(current_user, file_id)
        db.session.commit()
        flash("已移入回收站", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("files.index"))


# ---------- 批量操作 ----------

def _batch_run(func, ids, action_label):
    ok = fail = 0
    for fid in ids:
        try:
            func(fid)
            ok += 1
        except (ValueError, PermissionError, QuotaError):
            fail += 1
    db.session.commit()
    if ok:
        flash(f"已{action_label} {ok} 项" + (f"，{fail} 项失败" if fail else ""),
              "success" if fail == 0 else "warning")
    elif fail:
        flash(f"操作失败：{fail} 项不符合条件", "danger")
    return redirect(request.referrer or url_for("files.index"))


@files_bp.route("/files/batch/delete", methods=["POST"])
@login_required
def batch_delete():
    ids = _parse_ids(request.form.getlist("ids"))
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    return _batch_run(
        lambda fid: file_service.soft_delete(current_user, fid), ids, "移入回收站")


@files_bp.route("/files/batch/move", methods=["POST"])
@login_required
def batch_move():
    ids = _parse_ids(request.form.getlist("ids"))
    target = request.form.get("target_parent_id") or None
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    return _batch_run(
        lambda fid: file_service.move(current_user, fid, target), ids, "移动")


@files_bp.route("/files/batch/copy", methods=["POST"])
@login_required
def batch_copy():
    ids = _parse_ids(request.form.getlist("ids"))
    target = request.form.get("target_parent_id") or None
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))
    return _batch_run(
        lambda fid: file_service.copy(current_user, fid, target), ids, "复制")


@files_bp.route("/files/api/paste", methods=["POST"])
@login_required
def paste():
    """剪贴板粘贴：mode=copy 复制 / mode=cut 剪切并移动"""
    payload = request.get_json(silent=True) or {}
    ids = _parse_ids(payload.get("ids") or [])
    mode = payload.get("mode")
    target = payload.get("target")
    if mode not in ("copy", "cut"):
        return jsonify(ok=False, error="无效的操作类型"), 400
    if not ids:
        return jsonify(ok=False, error="剪贴板为空"), 400

    target = None if target in (None, "", 0, "0") else target
    if target is not None:
        try:
            target = int(target)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="目标文件夹无效"), 400

    fn = file_service.copy if mode == "copy" else file_service.move
    done, failed = 0, []
    for fid in ids:
        try:
            fn(current_user, fid, target)
            done += 1
        except (ValueError, PermissionError, QuotaError) as e:
            failed.append(str(e))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error="操作失败，请重试"), 500
    return jsonify(ok=True, done=done, failed=failed[:5])


@files_bp.route("/files/batch/download")
@login_required
def batch_download():
    """将选中的文件/文件夹打包为一个 zip 下载"""
    import zipfile
    import tempfile

    ids = _parse_ids(request.args.getlist("ids"))
    if not ids:
        flash("未选择文件", "danger")
        return redirect(request.referrer or url_for("files.index"))

    nodes = []
    for fid in ids:
        try:
            nodes.append(file_service.get_owned_file(current_user, fid))
        except PermissionError:
            continue
    if not nodes:
        abort(404, "所选文件不存在")

    total = sum(file_service.tree_size(current_user, n) for n in nodes)
    try:
        check_download(current_user, total)
    except QuotaError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("files.index"))

    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for n in nodes:
                if n.is_dir:
                    _add_dir_to_zip(zf, n, n.name + "/")
                else:
                    p = file_service.get_physical_path(n.storage_key)
                    if os.path.exists(p):
                        zf.write(p, n.name)
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise

    consume_traffic(current_user, total, "download")
    db.session.commit()

    @after_this_request
    def _cleanup(resp):
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return resp

    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name="files.zip")


# ---------- 分片上传 ----------

@files_bp.route("/api/upload/init", methods=["POST"])
@login_required
def upload_init():
    data = request.get_json() or {}
    filename = data.get("filename") or ""
    total_size = int(data.get("total_size") or 0)
    chunk_size = int(data.get("chunk_size") or current_app.config["CHUNK_SIZE"])
    md5 = data.get("md5")
    parent_id = data.get("parent_id") or None

    if not filename or total_size <= 0:
        return jsonify(error="参数错误"), 400

    try:
        check_upload(current_user, total_size)
    except QuotaError as e:
        return jsonify(error=str(e)), 400

    # 秒传：md5 命中且文件实体存在
    if md5:
        existing = File.query.filter_by(user_id=current_user.id, md5=md5, is_dir=False).first()
        if existing and existing.storage_key and os.path.exists(
                file_service.get_physical_path(existing.storage_key)):
            f = file_service.create_reference(
                current_user, parent_id, filename, total_size,
                mime=existing.mime, md5=md5, storage_key=existing.storage_key,
            )
            consume_traffic(current_user, total_size, "upload")
            db.session.commit()
            return jsonify(uploaded=True, fast=True, file_id=f.id)

    sess = UploadSession(
        user_id=current_user.id, parent_id=parent_id, filename=filename,
        total_size=total_size, chunk_size=chunk_size, md5=md5,
    )
    db.session.add(sess)
    db.session.commit()
    return jsonify(uploaded=False, upload_id=sess.upload_id, chunk_size=chunk_size)


@files_bp.route("/api/upload/chunk", methods=["POST"])
@login_required
def upload_chunk():
    upload_id = request.form.get("upload_id")
    index = int(request.form.get("index"))
    chunk = request.files.get("chunk")
    sess = UploadSession.query.filter_by(upload_id=upload_id, user_id=current_user.id).first()
    if sess is None or sess.status != "uploading":
        return jsonify(error="上传会话无效"), 400
    if chunk is None:
        return jsonify(error="缺少分片"), 400

    tmp_dir = os.path.join(current_app.config["UPLOAD_TMP_ROOT"], upload_id)
    os.makedirs(tmp_dir, exist_ok=True)
    chunk_path = os.path.join(tmp_dir, f"{index}.part")
    chunk.save(chunk_path)

    received = set(sess.received_chunks.split(",")) if sess.received_chunks else set()
    received.add(str(index))
    sess.received_chunks = ",".join(sorted(received, key=int))
    db.session.commit()
    return jsonify(ok=True, received=len(received))


@files_bp.route("/api/upload/complete", methods=["POST"])
@login_required
def upload_complete():
    data = request.get_json() or {}
    upload_id = data.get("upload_id")
    sess = UploadSession.query.filter_by(upload_id=upload_id, user_id=current_user.id).first()
    if sess is None:
        return jsonify(error="上传会话无效"), 400

    tmp_dir = os.path.join(current_app.config["UPLOAD_TMP_ROOT"], upload_id)
    merged = os.path.join(tmp_dir, "merged")
    with open(merged, "wb") as out:
        received = sorted([int(i) for i in sess.received_chunks.split(",") if i])
        for i in received:
            p = os.path.join(tmp_dir, f"{i}.part")
            with open(p, "rb") as fh:
                shutil_copyfileobj(fh, out)

    try:
        f = file_service.save_chunked_file(
            current_user, sess.parent_id, sess.filename, sess.total_size,
            merged, md5=sess.md5, mime=None
        )
        consume_traffic(current_user, sess.total_size, "upload")
        sess.status = "complete"
        sess.completed_at = utcnow()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify(error=str(e)), 500
    finally:
        shutil_rmtree(tmp_dir)

    return jsonify(ok=True, file_id=f.id)


def shutil_copyfileobj(src, dst):
    import shutil
    shutil.copyfileobj(src, dst)


def shutil_rmtree(p):
    import shutil
    if os.path.exists(p):
        shutil.rmtree(p, ignore_errors=True)


# ---------- 回收站 ----------

@files_bp.route("/trash")
@login_required
def trash():
    items = file_service.list_trash(current_user)
    return render_template("files/trash.html", items=items)


@files_bp.route("/files/<int:file_id>/restore", methods=["POST"])
@login_required
def restore(file_id):
    try:
        file_service.restore(current_user, file_id)
        db.session.commit()
        flash("已恢复", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(url_for("files.trash"))


@files_bp.route("/files/<int:file_id>/purge", methods=["POST"])
@login_required
def purge(file_id):
    try:
        file_service.hard_delete(current_user, file_id)
        flash("已彻底删除", "success")
    except (ValueError, PermissionError) as e:
        flash(str(e), "danger")
    return redirect(url_for("files.trash"))


@files_bp.route("/trash/empty", methods=["POST"])
@login_required
def empty_trash():
    n = file_service.empty_trash(current_user)
    flash(f"回收站已清空（{n} 项）", "success")
    return redirect(url_for("files.trash"))
