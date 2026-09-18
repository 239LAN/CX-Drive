"""计费服务：余额充值、会员与叠加包购买、兑换码生成与核销

金额一律使用 Decimal；扣款与权益开通在同一事务内完成。
会员购买按「精确月」顺延；赠送/兑换按「秒」顺延，两者共用同一套时长工具。
"""
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import (
    User, MembershipPlan, AddonPackage, UserMembership, UserAddon,
    RechargeRecord, BalanceLog, PurchaseOrder, RedeemCode, RedeemRecord,
)
from app.utils.helpers import add_months, human_duration, utcnow

# 兑换码字符集：去掉易混淆的 I/O/0/1
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


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


def _plan_base(user: User, now=None):
    """会员顺延基点：未到期则从现有到期时间续期，否则从现在开始"""
    now = now or utcnow()
    if user.vip_expire_at and user.vip_expire_at > now:
        return user.vip_expire_at
    return now


def _apply_plan(user: User, plan: MembershipPlan, expire_at, source: str):
    """写入会员权益与开通记录（不提交事务）"""
    user.plan_id = plan.id
    user.vip_expire_at = expire_at
    # 开通/续费即解锁容量锁定
    user.storage_locked = False
    user.locked_at = None
    db.session.add(UserMembership(user_id=user.id, plan_id=plan.id, start_at=utcnow(),
                                  expire_at=expire_at, source=source))


def purchase_plan(user: User, plan_id: int, months: int = 1):
    """购买会员：支持一次购买多个月（精确月），余额扣款"""
    plan = db.session.get(MembershipPlan, plan_id)
    if plan is None or plan.name == "free":
        raise BillingError("无效的会员套餐")
    try:
        months = int(months)
    except (TypeError, ValueError):
        raise BillingError("购买月数不正确")
    if months < 1:
        raise BillingError("购买月数必须大于 0")

    amount = (Decimal(plan.monthly_price) * months).quantize(Decimal("0.01"))
    _deduct_balance(user, amount, "purchase")

    expire_at = add_months(_plan_base(user), months)
    _apply_plan(user, plan, expire_at, "purchase")

    po = PurchaseOrder(user_id=user.id, item_type="plan", item_id=plan.id,
                       amount_paid=amount, status="paid")
    db.session.add(po)
    db.session.commit()


def purchase_addon(user: User, package_id: int):
    """购买叠加包，余额扣款"""
    pkg = db.session.get(AddonPackage, package_id)
    if pkg is None:
        raise BillingError("无效的叠加包")

    _deduct_balance(user, pkg.price, "purchase")

    now = utcnow()
    ua = UserAddon(user_id=user.id, package_id=pkg.id,
                   start_at=now, expire_at=now + timedelta(seconds=pkg.duration_seconds),
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


def admin_gift_plan(user: User, plan_id: int, seconds: int):
    """管理员免费赠送会员（秒级时长）"""
    plan = db.session.get(MembershipPlan, plan_id)
    if plan is None or plan.name == "free":
        raise BillingError("无效的会员套餐")
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        raise BillingError("赠送时长不正确")
    if seconds <= 0:
        raise BillingError("赠送时长必须大于 0")

    expire_at = _plan_base(user) + timedelta(seconds=seconds)
    _apply_plan(user, plan, expire_at, "admin_gift")
    db.session.commit()


def admin_gift_addon(user: User, package_id: int):
    """管理员免费赠送叠加包"""
    pkg = db.session.get(AddonPackage, package_id)
    if pkg is None:
        raise BillingError("无效的叠加包")
    now = utcnow()
    ua = UserAddon(user_id=user.id, package_id=pkg.id,
                   start_at=now, expire_at=now + timedelta(seconds=pkg.duration_seconds),
                   remaining_bytes=pkg.amount_bytes, status="active", source="admin_gift")
    db.session.add(ua)
    db.session.commit()


# ---------- 兑换码 ----------

def _unique_code(prefix: str, length: int) -> str:
    """生成不与库中重复的随机码"""
    length = max(6, min(int(length or 12), 32))
    for _ in range(20):
        code = f"{prefix}{''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))}"
        if RedeemCode.query.filter_by(code=code).first() is None:
            return code
    raise BillingError("兑换码生成失败，请重试")


def generate_redeem_codes(kind: str, count=1, prefix="", length=12, note="",
                          expire_days=0, plan_id=None, duration_seconds=None,
                          amount=None, package_id=None):
    """批量生成一次性兑换码，返回生成的 RedeemCode 对象列表

    kind = plan（会员，需套餐 + 秒级时长）/ balance（余额，需金额）/ addon（叠加包，时长可选）
    """
    if kind not in ("plan", "balance", "addon"):
        raise BillingError("兑换码类型不正确")
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise BillingError("生成数量不正确")
    if not 1 <= count <= 200:
        raise BillingError("单批生成数量需在 1 ~ 200 之间")

    plan = package = None
    if kind == "plan":
        plan = db.session.get(MembershipPlan, plan_id or 0)
        if plan is None or plan.name == "free":
            raise BillingError("请选择有效的会员套餐")
        try:
            duration_seconds = int(duration_seconds or 0)
        except (TypeError, ValueError):
            raise BillingError("会员时长不正确")
        if duration_seconds <= 0:
            raise BillingError("请填写有效的会员时长")
    elif kind == "addon":
        package = db.session.get(AddonPackage, package_id or 0)
        if package is None:
            raise BillingError("请选择有效的叠加包")
        try:
            duration_seconds = int(duration_seconds or 0)
        except (TypeError, ValueError):
            raise BillingError("叠加包时长不正确")
        # 不填时长时沿用叠加包自身时长
        duration_seconds = duration_seconds or package.duration_seconds
    else:
        try:
            amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            raise BillingError("请填写有效的余额金额")
        if amount <= 0:
            raise BillingError("余额金额必须大于 0")

    try:
        expire_days = int(expire_days or 0)
    except (TypeError, ValueError):
        raise BillingError("有效期天数不正确")
    if expire_days < 0:
        raise BillingError("有效期天数不能为负数")
    expire_at = utcnow() + timedelta(days=expire_days) if expire_days else None

    prefix = (prefix or "").strip().upper()
    batch = utcnow().strftime("%Y%m%d%H%M%S")
    codes = []
    for _ in range(count):
        codes.append(RedeemCode(
            code=_unique_code(prefix, length), kind=kind,
            plan_id=plan.id if plan else None,
            duration_seconds=duration_seconds,
            amount=amount,
            package_id=package.id if package else None,
            expire_at=expire_at, batch=batch, note=(note or "").strip() or None,
        ))
    db.session.add_all(codes)
    db.session.commit()
    return codes


def redeem_code(user: User, code_text: str) -> str:
    """核销兑换码（一次性），返回兑换内容描述"""
    code_text = (code_text or "").strip().upper()
    if not code_text:
        raise BillingError("请输入兑换码")
    code = RedeemCode.query.filter_by(code=code_text).first()
    if code is None:
        raise BillingError("兑换码不存在")
    if code.is_used:
        raise BillingError("该兑换码已被使用")
    if code.is_expired():
        raise BillingError("该兑换码已过期")

    now = utcnow()
    if code.kind == "plan":
        plan = code.plan
        seconds = int(code.duration_seconds or 0)
        if plan is None or seconds <= 0:
            raise BillingError("兑换码内容已失效")
        _apply_plan(user, plan, _plan_base(user, now) + timedelta(seconds=seconds), "redeem")
        detail = f"{plan.display_name} {human_duration(seconds)}"
    elif code.kind == "balance":
        money = Decimal(code.amount or 0)
        user.balance = Decimal(user.balance) + money
        _add_balance_log(user.id, money, user.balance, "redeem", code.code)
        detail = f"余额 {money} 元"
    else:
        pkg = code.package
        if pkg is None:
            raise BillingError("兑换码内容已失效")
        seconds = int(code.duration_seconds or pkg.duration_seconds)
        db.session.add(UserAddon(
            user_id=user.id, package_id=pkg.id, start_at=now,
            expire_at=now + timedelta(seconds=seconds),
            remaining_bytes=pkg.amount_bytes, status="active", source="redeem"))
        detail = f"{pkg.name} {human_duration(seconds)}"

    code.used_count = (code.used_count or 0) + 1
    code.used_at = now
    code.used_by = user.id
    db.session.add(RedeemRecord(code_id=code.id, code=code.code, user_id=user.id,
                                kind=code.kind, detail=detail))
    db.session.commit()
    return detail
