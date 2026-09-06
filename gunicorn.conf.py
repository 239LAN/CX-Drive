"""gunicorn 生产配置（供 install.sh 部署的 systemd 服务使用）"""
import multiprocessing
import os

# 监听地址，可用环境变量 CLOUDPAN_BIND 覆盖（写在 .env 中）
bind = os.environ.get("CLOUDPAN_BIND", "0.0.0.0:4280")

workers = int(os.environ["CLOUDPAN_WORKERS"]) if os.environ.get("CLOUDPAN_WORKERS") else \
    multiprocessing.cpu_count() * 2 + 1
timeout = 120
accesslog = "-"
errorlog = "-"


def when_ready(server):
    """仅在 gunicorn 主进程启动一次定时任务，避免多 worker 重复执行维护任务"""
    from app import create_app
    from app.scheduler import start_scheduler

    start_scheduler(create_app())
