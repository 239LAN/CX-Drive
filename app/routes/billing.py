"""计费路由：余额、购买会员/叠加包（充值暂未开放，余额仅管理员调整）"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.models import MembershipPlan, AddonPackage, BalanceLog
from app.services import billing_service
from app.services.billing_service import BillingError
from app.services.quota_service import effective_quota

billing_bp = Blueprint("billing", __name__)


@billing_bp.route("/billing")
@login_required
def index():
    plans = MembershipPlan.query.order_by(MembershipPlan.sort).all()
    addons = AddonPackage.query.order_by(AddonPackage.sort).all()
    q = effective_quota(current_user)
    logs = BalanceLog.query.filter_by(user_id=current_user.id).order_by(
        BalanceLog.created_at.desc()).limit(20).all()
    return render_template("billing/index.html", plans=plans, addons=addons,
                           q=q, logs=logs, balance=current_user.balance)


@billing_bp.route("/billing/purchase_plan", methods=["POST"])
@login_required
def purchase_plan():
    plan_id = request.form.get("plan_id")
    try:
        billing_service.purchase_plan(current_user, int(plan_id))
        flash("会员开通成功", "success")
    except BillingError as e:
        db_rollback()
        flash(str(e), "danger")
    return redirect(url_for("billing.index"))


@billing_bp.route("/billing/purchase_addon", methods=["POST"])
@login_required
def purchase_addon():
    package_id = request.form.get("package_id")
    try:
        billing_service.purchase_addon(current_user, int(package_id))
        flash("叠加包购买成功", "success")
    except BillingError as e:
        db_rollback()
        flash(str(e), "danger")
    return redirect(url_for("billing.index"))


def db_rollback():
    from app.extensions import db
    db.session.rollback()
