"""首页与通用页面"""
from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user, login_required

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("files.index"))
    return render_template("index.html")


@main_bp.route("/dashboard")
@login_required
def dashboard():
    from app.services.quota_service import effective_quota
    q = effective_quota(current_user)
    return render_template("dashboard.html", q=q)
