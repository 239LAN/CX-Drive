"""gunicorn 生产配置（供 install.sh 部署的 systemd 服务使用）"""
import multiprocessing
import os
import sys

# 进程工作目录即安装目录（systemd WorkingDirectory），借此定位站点配置模块
sys.path.insert(0, os.getcwd())

from config import load_site_config  # noqa: E402

_site = load_site_config()

# 监听地址，可用环境变量 CLOUDPAN_BIND 覆盖
bind = os.environ.get("CLOUDPAN_BIND", "0.0.0.0:4280")

# HTTPS：由 gunicorn 直接终止 TLS；证书缺失时 load_site_config 已自动关闭
if _site["HTTPS_ENABLED"]:
    certfile = _site["HTTPS_CERTFILE"]
    keyfile = _site["HTTPS_KEYFILE"]

workers = int(os.environ["CLOUDPAN_WORKERS"]) if os.environ.get("CLOUDPAN_WORKERS") else \
    multiprocessing.cpu_count() * 2 + 1
timeout = 120
accesslog = "-"
errorlog = "-"


def when_ready(server):
    """主进程启动定时任务，避免多 worker 重复执行"""
    from app import create_app
    from app.scheduler import start_scheduler

    start_scheduler(create_app())
