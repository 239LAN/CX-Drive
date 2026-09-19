"""远端取回等待页与进度查询

远端存储点上的文件需要先取回到本地缓存才能下载/预览。下载、预览、分享、直链
等入口在缓存未命中时统一跳到这里：页面轮询进度，取回完成后自动跳回原地址继续。
"""
from flask import (
    Blueprint, jsonify, redirect, render_template, request, url_for,
)

from app.services import transfer_service

transfer_bp = Blueprint("transfer", __name__)

# 等待页 URL 上携带原表单字段的前缀（见 waiting_redirect 的 method="post"）
_FIELD_PREFIX = "f_"


def current_target() -> str:
    """当前请求地址，用于取回完成后跳回继续下载"""
    return request.full_path.rstrip("?")


def waiting_redirect(tokens, next_url: str, title: str = "",
                     method: str = "get", fields=None):
    """跳转到「正在取回」等待页（tokens 可为单个 token 或列表）

    原入口是 POST（如分享下载、解压、压缩）时传 method="post" 与 fields，
    等待页取回完成后会用同样的表单字段重新提交，避免跳回时 Method Not Allowed。
    字段值由服务端从当次请求中原样带过来，抵达目标路由后仍会走各自的权限校验。
    """
    if isinstance(tokens, str):
        tokens = [tokens]
    params = {"tokens": ",".join(tokens), "next": next_url, "title": title}
    if method == "post":
        params["method"] = "post"
        for key, value in (fields or {}).items():
            if value is None:
                continue
            values = [value] if isinstance(value, str) else list(value)
            params[_FIELD_PREFIX + key] = values
    return redirect(url_for("transfer.wait", **params))


@transfer_bp.route("/transfer/wait")
def wait():
    """取回等待页：token 与回跳地址均由服务端生成，无需登录即可查看进度"""
    tokens = _tokens()
    target = _safe_next(request.args.get("next"))
    if not tokens or not target:
        return redirect(url_for("main.index"))
    post = request.args.get("method") == "post"
    return render_template("transfer/wait.html", tokens=",".join(tokens),
                           next_url=target, post_form=post,
                           fields=_fields() if post else {},
                           title=(request.args.get("title") or "文件")[:200])


@transfer_bp.route("/transfer/status")
def status():
    """进度轮询接口：tokens=a,b,c，返回聚合后的进度"""
    tokens = _tokens()
    if not tokens:
        return jsonify(error="缺少任务标识"), 400
    return jsonify(transfer_service.aggregate_status(tokens))


def _fields() -> dict:
    """还原等待页需要带回原入口的表单字段（含多值字段）"""
    out = {}
    for key in request.args:
        if not key.startswith(_FIELD_PREFIX):
            continue
        name = key[len(_FIELD_PREFIX):]
        out[name] = request.args.getlist(key)
    return out


def _tokens() -> list:
    raw = request.args.get("tokens") or ""
    return [t for t in (part.strip() for part in raw.split(",")) if t][:200]


def _safe_next(raw):
    """只允许跳回本站路径，避免被构造成开放重定向"""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    return raw
