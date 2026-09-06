"""定时任务：流量跨月重置、到期锁定、30 天缓冲期后硬删除"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import User, MembershipPlan, UserAddon
from app.services import file_service


def reset_monthly_traffic():
    now = datetime.utcnow()
    users = User.query.all()
    for u in users:
        if u.month_reset_at and (now.year, now.month) != (u.month_reset_at.year, u.month_reset_at.month):
            u.used_upload_month = 0
            u.used_download_month = 0
            u.month_reset_at = now
    db.session.commit()


def expire_memberships():
    """会员到期：回落到免费版并锁定容量"""
    now = datetime.utcnow()
    users = User.query.filter(User.vip_expire_at.is_not(None)).all()
    free = MembershipPlan.query.filter_by(name="free").first()
    for u in users:
        if u.vip_expire_at < now:
            u.plan_id = free.id if free else None
            u.vip_expire_at = None
            if not u.storage_locked:
                u.storage_locked = True
                u.locked_at = now
    db.session.commit()


def expire_addons():
    """叠加包到期标记为 expired"""
    now = datetime.utcnow()
    addons = UserAddon.query.filter_by(status="active").filter(UserAddon.expire_at <= now).all()
    for a in addons:
        a.status = "expired"
    db.session.commit()


def hard_delete_locked_users():
    """锁定超过缓冲期仍未续费的用户，硬删除其全部文件"""
    now = datetime.utcnow()
    threshold = now - timedelta(days=30)
    users = User.query.filter_by(storage_locked=True).all()
    for u in users:
        if u.locked_at and u.locked_at < threshold:
            file_service.purge_user_data(u)
            # 文件已清空，容量释放，解除锁定（用户回落免费版后可继续使用）
            u.storage_locked = False
            u.locked_at = None
    db.session.commit()


def run_all():
    reset_monthly_traffic()
    expire_memberships()
    expire_addons()
    hard_delete_locked_users()


def start_scheduler(app):
    """启动 APScheduler 定时任务"""
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()

    def job():
        with app.app_context():
            run_all()

    scheduler.add_job(job, "interval", hours=1, id="maintenance")
    scheduler.start()
    return scheduler
