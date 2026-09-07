"""数据模型定义"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db


def gen_uuid() -> str:
    return uuid.uuid4().hex


def now_utc() -> datetime:
    """当前 UTC 时间（naive）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(128), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)

    # 余额（单位：元，Decimal 精度 2）
    balance = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)

    # 会员状态
    plan_id = db.Column(db.Integer, db.ForeignKey("membership_plans.id"), nullable=True)
    vip_expire_at = db.Column(db.DateTime, nullable=True)
    # 到期锁定（容量到期后禁止上传/下载）
    storage_locked = db.Column(db.Boolean, default=False, nullable=False)
    locked_at = db.Column(db.DateTime, nullable=True)

    # 月流量统计
    month_reset_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    used_upload_month = db.Column(db.BigInteger, default=0, nullable=False)
    used_download_month = db.Column(db.BigInteger, default=0, nullable=False)

    files = db.relationship("File", backref="owner", lazy="dynamic")
    plan = db.relationship("MembershipPlan", foreign_keys=[plan_id])

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    def is_vip_or_svip(self) -> bool:
        return self.plan is not None and self.plan.name in ("vip", "svip")


class MembershipPlan(db.Model):
    """会员套餐"""
    __tablename__ = "membership_plans"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False)  # free/vip/svip
    display_name = db.Column(db.String(64), nullable=False)
    monthly_price = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    storage_quota_bytes = db.Column(db.BigInteger, nullable=False)
    max_file_size_bytes = db.Column(db.BigInteger, nullable=True)  # None = 不限
    speed_limit_bps = db.Column(db.BigInteger, nullable=True)      # None = 不限
    monthly_traffic_bytes = db.Column(db.BigInteger, nullable=True)  # None = 不限
    allow_preview = db.Column(db.Boolean, default=False, nullable=False)
    sort = db.Column(db.Integer, default=0, nullable=False)


class UserMembership(db.Model):
    """用户会员开通记录（用于追溯）"""
    __tablename__ = "user_memberships"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("membership_plans.id"), nullable=False)
    start_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    expire_at = db.Column(db.DateTime, nullable=False)
    source = db.Column(db.String(32), default="purchase")  # purchase/admin_gift
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class AddonPackage(db.Model):
    """叠加包（流量/容量）"""
    __tablename__ = "addon_packages"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(16), nullable=False)  # traffic / storage
    name = db.Column(db.String(64), nullable=False)
    amount_bytes = db.Column(db.BigInteger, nullable=False)
    price = db.Column(db.Numeric(12, 2), nullable=False)
    duration_days = db.Column(db.Integer, default=30, nullable=False)
    sort = db.Column(db.Integer, default=0, nullable=False)


class UserAddon(db.Model):
    """用户已购叠加包（有效期内）"""
    __tablename__ = "user_addons"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey("addon_packages.id"), nullable=False)
    start_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    expire_at = db.Column(db.DateTime, nullable=False)
    remaining_bytes = db.Column(db.BigInteger, nullable=False)
    status = db.Column(db.String(16), default="active")  # active / expired / used
    source = db.Column(db.String(32), default="purchase")

    package = db.relationship("AddonPackage")


class RechargeRecord(db.Model):
    """充值订单（支付 API 占位）"""
    __tablename__ = "recharge_records"

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(64), unique=True, default=gen_uuid, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    channel = db.Column(db.String(32), default="mock")  # mock/wechat/alipay
    status = db.Column(db.String(16), default="pending")  # pending/paid/failed
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    paid_at = db.Column(db.DateTime, nullable=True)


class BalanceLog(db.Model):
    """余额变动流水"""
    __tablename__ = "balance_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    change = db.Column(db.Numeric(12, 2), nullable=False)  # 正=增加，负=扣减
    balance_after = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(32), nullable=False)  # recharge/purchase/admin_adjust
    ref_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class PurchaseOrder(db.Model):
    """购买订单（会员/叠加包）"""
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(64), unique=True, default=gen_uuid, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    item_type = db.Column(db.String(16), nullable=False)  # plan / addon
    item_id = db.Column(db.Integer, nullable=False)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(16), default="paid")
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class File(db.Model):
    """文件/文件夹"""
    __tablename__ = "files"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=True, index=True)
    name = db.Column(db.String(512), nullable=False)
    is_dir = db.Column(db.Boolean, default=False, nullable=False)
    size = db.Column(db.BigInteger, default=0, nullable=False)
    mime = db.Column(db.String(128), nullable=True)
    # 物理存储名（文件实体），目录为 None
    storage_key = db.Column(db.String(64), nullable=True, index=True)
    md5 = db.Column(db.String(32), nullable=True, index=True)
    # 软删除（回收站）
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_original_parent_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    children = db.relationship("File", backref=db.backref("parent", remote_side=[id]))


class ShareLink(db.Model):
    """分享外链"""
    __tablename__ = "share_links"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, default=gen_uuid, nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    expire_at = db.Column(db.DateTime, nullable=True)
    max_downloads = db.Column(db.Integer, nullable=True)
    download_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)

    file = db.relationship("File", foreign_keys=[file_id])


class UploadSession(db.Model):
    """分片上传会话"""
    __tablename__ = "upload_sessions"

    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.String(64), unique=True, default=gen_uuid, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=True)
    filename = db.Column(db.String(512), nullable=False)
    total_size = db.Column(db.BigInteger, nullable=False)
    chunk_size = db.Column(db.BigInteger, nullable=False)
    md5 = db.Column(db.String(32), nullable=True)
    # 已接收的分片索引，逗号分隔
    received_chunks = db.Column(db.Text, default="", nullable=False)
    status = db.Column(db.String(16), default="uploading")  # uploading/complete
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)


class AuditLog(db.Model):
    """审计日志"""
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(64), nullable=False)
    target = db.Column(db.String(256), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)


class AnonDlWeek(db.Model):
    """匿名下载周流量计数（按 IP+自然周，限制单 IP 每周配额）"""
    __tablename__ = "anon_dl_weeks"
    __table_args__ = (db.UniqueConstraint("ip", "week", name="uq_anon_dl_ip_week"),)

    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(64), nullable=False, index=True)
    week = db.Column(db.String(8), nullable=False)  # ISO 周键，如 "2026-W36"
    bytes_used = db.Column(db.BigInteger, default=0, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc, nullable=False)


class RemoteDownload(db.Model):
    """远程下载任务"""
    __tablename__ = "remote_downloads"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=True)
    url = db.Column(db.Text, nullable=False)
    filename = db.Column(db.String(512), nullable=True)
    total_size = db.Column(db.BigInteger, nullable=True)   # 未知时为空，下载中按需回填
    downloaded_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    status = db.Column(db.String(16), default="queued", nullable=False, index=True)
    # queued / downloading / done / failed / canceled
    error = db.Column(db.String(512), nullable=True)
    storage_key = db.Column(db.String(64), nullable=True)  # 完成后生成的物理文件 key
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    last_progress_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
