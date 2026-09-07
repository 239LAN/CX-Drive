"""认证路由：注册/登录/登出/改密码"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db
from app.models import User
from app.utils.helpers import safe_filename

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("files.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip() or None
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        error = None
        if not username or len(username) < 3:
            error = "用户名至少 3 个字符"
        elif len(password) < 6:
            error = "密码至少 6 个字符"
        elif password != confirm:
            error = "两次密码不一致"
        elif User.query.filter_by(username=username).first():
            error = "用户名已存在"
        elif email and User.query.filter_by(email=email).first():
            error = "邮箱已被注册"

        if error:
            flash(error, "danger")
            return render_template("auth/register.html", username=username, email=email)

        # 新用户默认免费版
        from app.models import MembershipPlan
        free = MembershipPlan.query.filter_by(name="free").first()
        user = User(username=username, email=email, plan_id=free.id if free else None)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("注册成功，请登录", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("files.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(password):
            flash("用户名或密码错误", "danger")
        elif not user.is_active:
            flash("账号已被禁用", "danger")
        else:
            login_user(user, remember=bool(request.form.get("remember")))
            flash("登录成功", "success")
            # 仅允许跳转站内相对路径
            next_url = request.args.get("next") or ""
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("files.index"))
    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("已退出登录", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm") or ""
        if not current_user.check_password(old):
            flash("原密码错误", "danger")
        elif len(new) < 6:
            flash("新密码至少 6 个字符", "danger")
        elif new != confirm:
            flash("两次密码不一致", "danger")
        else:
            current_user.set_password(new)
            db.session.commit()
            flash("密码修改成功", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/change_password.html")
