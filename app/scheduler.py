"""定时任务：流量跨月重置、到期锁定、30 天缓冲期后硬删除、缓存/丢失记录清理、每日 0 点检查更新"""
import threading
from datetime import timedelta

from app.extensions import db
from app.models import User, MembershipPlan, UserAddon
from app.services import file_service, storage_service, update_service
from app.utils.helpers import utcnow
from config import env_get


def reset_monthly_traffic():
    now = utcnow()
    users = User.query.all()
    for u in users:
        if u.month_reset_at and (now.year, now.month) != (u.month_reset_at.year, u.month_reset_at.month):
            u.used_upload_month = 0
            u.used_download_month = 0
            u.month_reset_at = now
    db.session.commit()


def expire_memberships():
    """会员到期：回落到免费版并锁定容量"""
    now = utcnow()
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
    now = utcnow()
    addons = UserAddon.query.filter_by(status="active").filter(UserAddon.expire_at <= now).all()
    for a in addons:
        a.status = "expired"
    db.session.commit()


def hard_delete_locked_users():
    """锁定超过缓冲期仍未续费的用户，硬删除其全部文件"""
    now = utcnow()
    threshold = now - timedelta(days=30)
    users = User.query.filter_by(storage_locked=True).all()
    for u in users:
        if u.locked_at and u.locked_at < threshold:
            file_service.purge_user_data(u)
            # 文件已清空、容量释放，解除锁定
            u.storage_locked = False
            u.locked_at = None
    db.session.commit()


def clean_storage_cache():
    """清理超期的 FTP 本地缓存文件"""
    try:
        storage_service.clean_cache()
    except OSError:
        pass


def purge_lost_files():
    """清理超过保留期（7 天）的已丢失文件记录"""
    storage_service.purge_lost_records()


def run_all():
    reset_monthly_traffic()
    expire_memberships()
    expire_addons()
    hard_delete_locked_users()
    clean_storage_cache()
    purge_lost_files()


def check_update():
    """检查新版本；config.yml 的 update.enabled 为 true 时自动安装"""
    update_service.check()


def start_scheduler(app):
    """启动 APScheduler 定时任务"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = BackgroundScheduler()

    def job():
        with app.app_context():
            run_all()

    def update_job():
        with app.app_context():
            check_update()

    scheduler.add_job(job, "interval", hours=1, id="maintenance")
    # 每日定时（默认 0 点，可在管理后台设置页调整）检查更新：
    # 关闭自动更新时仍检查，仅不安装
    scheduler.add_job(
        update_job,
        CronTrigger(hour=app.config.get("UPDATE_HOUR", 0),
                    minute=app.config.get("UPDATE_MINUTE", 0)),
        id="auto_update",
    )
    scheduler.start()
    if env_get("DISABLE_BG") != "1":
        # 启动后立即检查一次，页脚无需等到次日 0 点才显示版本信息
        threading.Thread(target=update_job, daemon=True, name="update-check").start()
    return scheduler
