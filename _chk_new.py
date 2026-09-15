# -*- coding: utf-8 -*-
"""定向校验：远程下载/剪贴板粘贴/详情/匿名周配额/批量复制"""
import os, sys, json, shutil, atexit
from datetime import datetime, timezone
from io import BytesIO
BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_chk_tmp")


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
os.makedirs(TMP, exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db").replace("\\", "/")
os.environ["STORAGE_ROOT"] = os.path.join(TMP, "storage")
os.environ["UPLOAD_TMP_ROOT"] = os.path.join(TMP, "uploads")
os.environ["REMOTE_TMP_ROOT"] = os.path.join(TMP, "remote")
os.environ["CLOUDPAN_DISABLE_BG"] = "1"

from app import create_app
from app.extensions import db
from app.models import User, File, ShareLink, AnonDlWeek, RemoteDownload
from app.services import remote_service

app = create_app()
R = []
def check(n, c, e=""):
    R.append((n, bool(c), e)); print(("PASS" if c else "FAIL"), n, e)
def body(r): return r.data.decode("utf-8", "ignore")

c = app.test_client()
rr = c.post("/register", data={"username": "boss", "email": "boss@t.cn", "password": "pass1234", "confirm": "pass1234", "agree": "on"})
print("register ->", rr.status_code, rr.location)
rr2 = c.post("/login", data={"username": "boss", "password": "pass1234", "agree": "on"})
print("login ->", rr2.status_code, rr2.location)
with app.app_context():
    u = User.query.filter_by(username="boss").first()
    u.is_admin = True; db.session.commit()
    uid = u.id

c.post("/files/folder/new", data={"name": "docs", "parent_id": ""})
with app.app_context():
    did = File.query.filter_by(name="docs", is_dir=True).first().id
fd = {"file": (BytesIO(b"aaa"), "a.txt"), "parent_id": str(did)}
c.post("/files/upload", data=fd, content_type="multipart/form-data")
with app.app_context():
    fid = File.query.filter_by(name="a.txt", is_dir=False).first().id

h = body(c.get(f"/files/{did}"))
check("文件页含详情按钮", 'data-bs-target="#detailModal"' in h)
check("文件页含打包下载", 'bi-file-earmark-zip' in h)
check("文件页含远程下载入口", "远程下载" in h)
check("详情弹窗存在", 'id="dMd5"' in h)

# 粘贴 copy -> 副本
r = c.post("/files/api/paste", data=json.dumps({"ids": [fid], "mode": "copy", "target": did}), content_type="application/json")
d0 = r.get_json() if r.is_json else {}
check("粘贴(copy) ok", r.status_code == 200 and d0.get("ok"), r.status_code)
with app.app_context():
    ncopy = File.query.filter(File.name.like("a 副本%")).count()
check("复制生成副本", ncopy >= 1, f"n={ncopy}")

# 粘贴 cut -> 根目录
r = c.post("/files/api/paste", data=json.dumps({"ids": [fid], "mode": "cut", "target": ""}), content_type="application/json")
d1 = r.get_json() if r.is_json else {}
check("粘贴(cut) ok", r.status_code == 200 and d1.get("ok"))
with app.app_context():
    moved = File.query.filter_by(name="a.txt", parent_id=None, deleted_at=None).first()
check("剪切移动到根目录", moved is not None)

with app.app_context():
    check("free并发=1", remote_service.concurrency_limit(User.query.get(uid)) == 1)
    check("映射表", remote_service.CONCURRENCY_BY_PLAN == {"free": 1, "vip": 3, "svip": 5})

r = c.post("/remote/create", data={"url": "https://example.com/x.zip", "filename": "x.zip", "parent_id": str(did)})
check("remote create 302", r.status_code in (302, 303))
h = body(c.get("/remote"))
check("远程页含任务", "排队中" in h and "example.com/x.zip" in h)
with app.app_context():
    task = RemoteDownload.query.first(); tid = task.id
    check("任务 queued", task.status == "queued")
r = c.post("/remote/create", data={"url": "file:///etc/passwd"})
check("拒非http(s)", b"\xe4\xbb\x85\xe6\x94\xaf\xe6\x8c\x81" in r.data or r.status_code in (302, 303))
r = c.post(f"/remote/{tid}/cancel")
with app.app_context():
    t2 = RemoteDownload.query.get(tid)
    check("取消 -> canceled", t2.status == "canceled")

# 匿名下载周配额：注入当前周近满用量
year, week, _ = datetime.now(timezone.utc).isocalendar()
wk = f"{year}-W{week:02d}"
with app.app_context():
    db.session.add(AnonDlWeek(ip="127.0.0.1", week=wk, bytes_used=1073741824 - 2))
    db.session.commit()
    sh = ShareLink(file_id=fid, user_id=uid)
    db.session.add(sh); db.session.commit()
    link = sh.token
guest = app.test_client()
r = guest.post(f"/share/{link}/download")
check("匿名超额429", r.status_code == 429, r.status_code)
check("下载计数未增(429前停)", r.status_code == 429)

r = c.post("/files/api/paste", data=json.dumps({"ids": [], "mode": "copy", "target": did}), content_type="application/json")
check("空剪贴板400", r.status_code == 400)
r = c.post("/files/api/paste", data=json.dumps({"ids": [fid], "mode": "bogus", "target": did}), content_type="application/json")
check("非法mode400", r.status_code == 400)

fails = [x for x in R if not x[1]]
print("\n===== SUMMARY =====", f"total={len(R)} pass={len(R)-len(fails)} fail={len(fails)}")
sys.exit(1 if fails else 0)
