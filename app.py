"""应用入口"""
from app import create_app

app = create_app()


if __name__ == "__main__":
    import os
    from app.scheduler import start_scheduler
    # debug 模式（reloader）下仅子进程启动调度器，避免双份定时任务
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler(app)
    app.run(host="0.0.0.0", port=5000, debug=True)
