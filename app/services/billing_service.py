"""计费服务：余额充值、会员与叠加包购买

金额一律使用 Decimal；扣款与权益开通在同一事务内完成。
"""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    User, MembershipPlan, AddonPackage, UserMembership, UserAddon,
    RechargeRecord, BalanceLog, PurchaseOrder,
)
from app.utils.helpers import utcnow


class BillingError(Exception):
    pass


def _add_balance_log(user_id, change: Decimal, balance_after: Decimal, reason: str, ref_id=None):
    log = BalanceLog(user_id=user_id, change=change, balance_after=balance_after,
                     reason=reason, ref_id=ref_id)
    db.session.add(log)


def recharge(user: User, amount: Decimal) -> RechargeRecord:
    """发起充值：生成充值记录并同步完成支付"""
    amount = Decimal(str(amount))
    if amount <= 0:
        raise BillingError("充值金额必须大于 0")

    rec = RechargeRecord(user_id=user.id, amount=amount, channel="mock", status="pending")
    db.session.add(rec)
    db.session.flush()

    _mock_pay_success(rec)
    db.session.commit()
    return rec


def _mock_pay_success(rec: RechargeRecord):
    """将充值记录置为已支付并增加余额"""
    rec.status = "paid"
    rec.paid_at = utcnow()
    user = db.session.get(User, rec.user_id)
    user.balance = Decimal(user.balance) + rec.amount
    _add_balance_log(user.id, rec.amount, user.balance, "recharge", rec.order_no)


def _deduct_balance(user: User, amount: Decimal, reason: str, ref_id=None):
    """扣减余额，不足抛异常"""
    amount = Decimal(str(amount))
    if Decimal(user.balance) < amount:
        raise BillingError("余额不足，请先充值")
    user.balance = Decimal(user.balance) - amount
    _add_balance_log(user.id, -amount, user.balance, reason, ref_id)


def purchase_plan(user: User, plan_id: int):
    """购买会员（月付），余额扣款"""
    plan = db.session.get(MembershipPlan, plan_id)
    if plan is None or plan.name == "free":
        raise BillingError("无效的会员套餐")

    _deduct_balance(user, plan.monthly_price, "purchase")

    # 有效期：会员未到期则从现有到期时间顺延 30 天
    now = utcnow()
    base = user.vip_expire_at if user.vip_expire_at and user.vip_expire_at > now else now
    expire_at = base + timedelta(days=30)

    user.plan_id = plan.id
    user.vip_expire_at = expire_at
    # 续费即解锁容量锁定
    user.storage_locked = False
    user.locked_at = None

    um = UserMembership(user_id=user.id, plan_id=plan.id,
                        start_at=now, expire_at=expire_at, source="purchase")
    po = PurchaseOrder(user_id=user.id, item_type="plan", item_id=plan.id,
                       amount_paid=plan.monthly_price, status="paid")
    db.session.add_all([um, po])
    db.session.commit()


def purchase_addon(user: User, package_id: int):
    """购买叠加包，余额扣款"""
    pkg = db.session.get(AddonPackage, package_id)
    if pkg is None:
        raise BillingError("无效的叠加包")

    _deduct_balance(user, pkg.price, "purchase")

    now = utcnow()
    expire_at = now + timedelta(days=pkg.duration_days)
    ua = UserAddon(user_id=user.id, package_id=pkg.id,
                   start_at=now, expire_at=expire_at,
                   remaining_bytes=pkg.amount_bytes, status="active", source="purchase")
    po = PurchaseOrder(user_id=user.id, item_type="addon", item_id=pkg.id,
                       amount_paid=pkg.price, status="paid")
    db.session.add_all([ua, po])
    db.session.commit()


# ---------- 管理员操作 ----------

def admin_adjust_balance(user: User, delta: Decimal, reason_note: str = ""):
    """管理员调整余额（可正可负）"""
    delta = Decimal(str(delta))
    user.balance = Decimal(user.balance) + delta
    _add_balance_log(user.id, delta, user.balance, "admin_adjust", reason_note)
    db.session.commit()


def admin_gift_plan(user: User, plan_id: int):
    """管理员免费赠送会员"""
    plan = db.session.get(MembershipPlan, plan_id)
    if plan is None or plan.name == "free":
        raise BillingError("无效的会员套餐")

    now = utcnow()
    base = user.vip_expire_at if user.vip_expire_at and user.vip_expire_at > now else now
    expire_at = base + timedelta(days=30)

    user.plan_id = plan.id
    user.vip_expire_at = expire_at
    user.storage_locked = False
    user.locked_at = None

    um = UserMembership(user_id=user.id, plan_id=plan.id,
                        start_at=now, expire_at=expire_at, source="admin_gift")
    db.session.add(um)
    db.session.commit()


def admin_gift_addon(user: User, package_id: int):
    """管理员免费赠送叠加包"""
    pkg = db.session.get(AddonPackage, package_id)
    if pkg is None:
        raise BillingError("无效的叠加包")
    now = utcnow()
    ua = UserAddon(user_id=user.id, package_id=pkg.id,
                   start_at=now, expire_at=now + timedelta(days=pkg.duration_days),
                   remaining_bytes=pkg.amount_bytes, status="active", source="admin_gift")
    db.session.add(ua)
    db.session.commit()
