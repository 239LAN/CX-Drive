# -*- coding: utf-8 -*-
"""远程下载真实 Worker 端到端测试：本地 HTTP 源 -> 任务完成 -> 落 File。

测试放行 _is_blocked_host 以覆盖完整下载链路，其 SSRF 拦截行为在末尾单独断言。
"""
import os, sys, time, threading, shutil, atexit
from http.server import HTTPServer, SimpleHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_rt_tmp")


def cleanup():
    """退出时清理临时目录与字节码缓存，避免残留"""
    os.chdir(BASE)
    try:
        from app.extensions import db
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)
    for root, dirs, _files in os.walk(BASE):
        for name in [d for d in dirs if d == "__pycache__"]:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            dirs.remove(name)


atexit.register(cleanup)

shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "www"), exist_ok=True)

# 构造 512KB 测试文件
PAYLOAD = os.urandom(512 * 1024)
with open(os.path.join(TMP, "www", "sample.bin"), "wb") as fh:
    fh.write(PAYLOAD)

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db").replace("\\", "/")
os.environ["STORAGE_ROOT"] = os.path.join(TMP, "storage")
os.environ["UPLOAD_TMP_ROOT"] = os.path.join(TMP, "uploads")
os.environ["REMOTE_TMP_ROOT"] = os.path.join(TMP, "remote")
# 不设置 CLOUDPAN_DISABLE_BG，以运行真实后台 Worker

from app import create_app
from app.extensions import db
from app.models import User, File, RemoteDownload, MembershipPlan
from app.services import remote_service

# 放行全部地址以覆盖完整下载链路
_orig_block = remote_service._is_blocked_host
remote_service._is_blocked_host = lambda url: False

# 本地 HTTP 服务
srv = HTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
port = srv.server_address[1]
srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
srv_thread.start()
os.chdir(os.path.join(TMP, "www"))  # 让处理器的 cwd 指向测试目录

app = create_app()
with app.app_context():
    free = MembershipPlan.query.filter_by(name="free").first()
    u = User(username="rtuser", email="rt@t.cn", password_hash="x", plan_id=free.id)
    db.session.add(u); db.session.commit()
    task = RemoteDownload(user_id=u.id, url=f"http://127.0.0.1:{port}/sample.bin",
                          filename="sample.bin")
    db.session.add(task); db.session.commit()
    tid = task.id

# 轮询等待 Worker 完成
ok = False
for _ in range(30):
    time.sleep(0.8)
    with app.app_context():
        t = db.session.get(RemoteDownload, tid)
        if t and t.status in ("done", "failed"):
            ok = t.status == "done"
            print("status =", t.status, "error =", t.error)
            if ok:
                f = File.query.get(t.file_id)
                print("file =", f.name, "size =", f.size,
                      "md5len =", len(f.md5 or ""))
                p = os.path.join(TMP, "storage", f.storage_key[:2], f.storage_key)
                print("disk exists =", os.path.exists(p), "match =", os.path.getsize(p) == len(PAYLOAD))
                ok = os.path.exists(p) and os.path.getsize(p) == len(PAYLOAD)
            break
# 还原防护后单独断言 SSRF 拦截行为
remote_service._is_blocked_host = _orig_block
ssrf_cases = [
    ("http://127.0.0.1/x", True), ("http://localhost/x", True),
    ("http://192.168.1.5/x", True), ("http://10.0.0.2/x", True),
    ("http://172.16.0.3/x", True), ("http://169.254.1.1/x", True),
    ("http://[::1]/x", True), ("https://example.com/x", False),
]
ssrf_ok = all(remote_service._is_blocked_host(u) is expect for u, expect in ssrf_cases)
print("SSRF blocked cases:", ssrf_ok, [u for u, e in ssrf_cases
      if remote_service._is_blocked_host(u) is not e])
print("RESULT:", "PASS" if (ok and ssrf_ok) else "FAIL")
srv.shutdown()
sys.exit(0 if (ok and ssrf_ok) else 1)
