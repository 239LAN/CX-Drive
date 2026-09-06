"""管理后台路由：概览统计、用户管理、套餐/叠加包维护、管理员授权"""
from datetime import datetime, timedelta
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    User, MembershipPlan, AddonPackage, File, ShareLink, BalanceLog,
    UserMembership, UserAddon, PurchaseOrder,
)
from app.services import billing_service
from app.services.billing_service import BillingError
from app.services.quota_service import effective_quota
from app.utils.helpers import admin_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

GB = 1024 ** 3
MB = 1024 ** 2


@admin_bp.before_request
@login_required
@admin_required
def _guard():
    pass


# ---------- 概览 ----------

def _dashboard_stats():
    now = datetime.utcnow()
    users = User.query.all()
    files = File.query.all()

    plan_ids = {p.id for p in MembershipPlan.query.all()}
    dist = {}
    for u in users:
        dist.setdefault(u.plan_id if u.plan_id in plan_ids else None, 0)
        dist[u.plan_id if u.plan_id in plan_ids else None] += 1

    return {
        "total_users": len(users),
        "active_users": sum(1 for u in users if u.is_active),
        "admin_users": sum(1 for u in users if u.is_admin),
        "disabled_users": sum(1 for u in users if not u.is_active),
        "locked_users": sum(1 for u in users if u.storage_locked),
        "expiring_soon": sum(1 for u in users if u.vip_expire_at and now < u.vip_expire_at <= now + timedelta(days=7)),
        "storage_used": sum(f.size for f in files if not f.is_dir),
        "file_count": sum(1 for f in files if not f.is_dir),
        "dir_count": sum(1 for f in files if f.is_dir),
        "share_count": ShareLink.query.count(),
        "plan_dist": dist,
        "recharge_today": db.session.query(db.func.coalesce(db.func.sum(BalanceLog.change), 0))
            .filter(BalanceLog.reason == "recharge",
                    BalanceLog.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0)).scalar(),
    }


@admin_bp.route("/")
def index():
    stats = _dashboard_stats()
    users = User.query.order_by(User.id.desc()).all()
    return render_template("admin/index.html", users=users, stats=stats,
                           plans=MembershipPlan.query.order_by(MembershipPlan.sort).all(),
                           addon_pkgs=AddonPackage.query.order_by(AddonPackage.sort).all())


# ---------- 用户详情与记录 ----------

@admin_bp.route("/user/<int:user_id>")
def user_detail(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    q = effective_quota(user)
    addons = db.session.query(UserAddon, AddonPackage.name, AddonPackage.type) \
        .join(AddonPackage, UserAddon.package_id == AddonPackage.id) \
        .filter(UserAddon.user_id == user.id) \
        .order_by(UserAddon.id.desc()).limit(10).all()
    memberships = db.session.query(UserMembership, MembershipPlan.display_name) \
        .join(MembershipPlan, UserMembership.plan_id == MembershipPlan.id) \
        .filter(UserMembership.user_id == user.id) \
        .order_by(UserMembership.id.desc()).limit(10).all()
    logs = BalanceLog.query.filter_by(user_id=user.id).order_by(BalanceLog.id.desc()).limit(30).all()
    orders = PurchaseOrder.query.filter_by(user_id=user.id).order_by(PurchaseOrder.id.desc()).limit(10).all()
    shares = ShareLink.query.filter_by(user_id=user.id).order_by(ShareLink.id.desc()).limit(10).all()
    files_total = File.query.filter_by(user_id=user.id, is_dir=False).count()
    files_deleted = File.query.filter_by(user_id=user.id, is_dir=False).filter(
        File.deleted_at.is_not(None)).count()
    return render_template("admin/user_detail.html", u=user, q=q, addons=addons,
                           memberships=memberships, logs=logs, orders=orders,
                           shares=shares, files_total=files_total, files_deleted=files_deleted,
                           plans=MembershipPlan.query.order_by(MembershipPlan.sort).all(),
                           addon_pkgs=AddonPackage.query.order_by(AddonPackage.sort).all())


# ---------- 余额/会员/叠加包 赠送 ----------

@admin_bp.route("/user/<int:user_id>/balance", methods=["POST"])
def adjust_balance(user_id):
    user = db.session.get(User, user_id)
    delta = request.form.get("delta")
    note = request.form.get("note") or ""
    try:
        billing_service.admin_adjust_balance(user, Decimal(delta), note)
        flash(f"已调整 {user.username} 余额", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"调整失败：{e}", "danger")
    return redirect(request.referrer or url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/user/<int:user_id>/gift_plan", methods=["POST"])
def gift_plan(user_id):
    user = db.session.get(User, user_id)
    plan_id = request.form.get("plan_id")
    try:
        billing_service.admin_gift_plan(user, int(plan_id))
        flash(f"已为 {user.username} 赠送会员", "success")
    except BillingError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/user/<int:user_id>/gift_addon", methods=["POST"])
def gift_addon(user_id):
    user = db.session.get(User, user_id)
    package_id = request.form.get("package_id")
    try:
        billing_service.admin_gift_addon(user, int(package_id))
        flash(f"已为 {user.username} 赠送叠加包", "success")
    except BillingError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(request.referrer or url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/user/<int:user_id>/toggle", methods=["POST"])
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if user.id != current_user.id:
        user.is_active = not user.is_active
        db.session.commit()
        flash(f"已{'启用' if user.is_active else '禁用'} {user.username}", "success")
    return redirect(request.referrer or url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/user/<int:user_id>/toggle_admin", methods=["POST"])
def toggle_admin(user_id):
    """提升/撤销管理员；保护：不能撤自己，不能撤最后一个管理员"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.is_admin:
        if user.id == current_user.id:
            flash("不能取消自己的管理员身份", "danger")
        elif User.query.filter_by(is_admin=True).count() <= 1:
            flash("系统至少需要保留一名管理员", "danger")
        else:
            user.is_admin = False
            db.session.commit()
            flash(f"已撤销 {user.username} 的管理员身份", "success")
    else:
        user.is_admin = True
        db.session.commit()
        flash(f"已将 {user.username} 提升为管理员", "success")
    return redirect(request.referrer or url_for("admin.index"))


# ---------- 会员套餐维护 ----------

@admin_bp.route("/plans")
def plans_page():
    return render_template("admin/plans.html",
                           plans=MembershipPlan.query.order_by(MembershipPlan.sort).all())


@admin_bp.route("/plans/save", methods=["POST"])
def plan_save():
    try:
        plan_id = request.form.get("plan_id")
        p = db.session.get(MembershipPlan, int(plan_id)) if plan_id else MembershipPlan()
        name = (request.form.get("name") or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        p.name = name.lower().replace(" ", "_")
        p.display_name = (request.form.get("display_name") or "").strip() or name
        p.monthly_price = Decimal(request.form.get("monthly_price") or 0)
        if p.monthly_price < 0:
            raise ValueError("价格不能为负数")
        p.storage_quota_bytes = int(float(request.form.get("storage_gb") or 0) * GB)
        if p.storage_quota_bytes <= 0:
            raise ValueError("容量必须大于 0")
        max_file_mb = int(float(request.form.get("max_file_mb") or 0) * MB)
        speed_mbps = int(float(request.form.get("speed_mbps") or 0) * 1024 ** 2)
        traffic_gb = int(float(request.form.get("traffic_gb") or 0) * GB)
        p.max_file_size_bytes = max_file_mb or None
        p.speed_limit_bps = speed_mbps or None
        p.monthly_traffic_bytes = traffic_gb or None
        p.allow_preview = bool(request.form.get("allow_preview"))
        p.sort = int(request.form.get("sort") or 0)
        if p.name == "free":
            p.monthly_price = Decimal("0.00")
        db.session.add(p)
        db.session.commit()
        flash("套餐已保存", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"保存失败：{e}", "danger")
    return redirect(url_for("admin.plans_page"))


@admin_bp.route("/plans/<int:plan_id>/delete", methods=["POST"])
def plan_delete(plan_id):
    p = db.session.get(MembershipPlan, plan_id)
    if p is None:
        abort(404)
    if p.name == "free":
        flash("免费套餐不可删除", "danger")
    elif db.session.query(User).filter_by(plan_id=p.id).first() or \
            db.session.query(UserMembership).filter_by(plan_id=p.id).first():
        flash("该套餐已被用户使用，无法删除", "danger")
    else:
        db.session.delete(p)
        db.session.commit()
        flash("套餐已删除", "success")
    return redirect(url_for("admin.plans_page"))


# ---------- 叠加包维护 ----------

@admin_bp.route("/addons")
def addons_page():
    return render_template("admin/addons.html",
                           addons=AddonPackage.query.order_by(AddonPackage.sort).all())


@admin_bp.route("/addons/save", methods=["POST"])
def addon_save():
    try:
        pkg_id = request.form.get("package_id")
        a = db.session.get(AddonPackage, int(pkg_id)) if pkg_id else AddonPackage()
        a.type = request.form.get("type") or "storage"
        if a.type not in ("traffic", "storage"):
            raise ValueError("类型无效")
        a.name = (request.form.get("name") or "").strip()
        if not a.name:
            raise ValueError("名称不能为空")
        a.amount_bytes = int(float(request.form.get("amount_gb") or 0) * GB)
        if a.amount_bytes <= 0:
            raise ValueError("数量必须大于 0")
        a.price = Decimal(request.form.get("price") or 0)
        if a.price < 0:
            raise ValueError("价格不能为负数")
        a.duration_days = int(request.form.get("duration_days") or 30)
        a.sort = int(request.form.get("sort") or 0)
        db.session.add(a)
        db.session.commit()
        flash("叠加包已保存", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"保存失败：{e}", "danger")
    return redirect(url_for("admin.addons_page"))


@admin_bp.route("/addons/<int:pkg_id>/delete", methods=["POST"])
def addon_delete(pkg_id):
    a = db.session.get(AddonPackage, pkg_id)
    if a is None:
        abort(404)
    if db.session.query(UserAddon).filter_by(package_id=a.id).first():
        flash("该叠加包已有用户持有，无法删除", "danger")
    else:
        db.session.delete(a)
        db.session.commit()
        flash("叠加包已删除", "success")
    return redirect(url_for("admin.addons_page"))
