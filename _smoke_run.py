# -*- coding: utf-8 -*-
"""整体回归冒烟测试：隔离临时 DB/存储，验证用户端+分享+管理端全链路渲染与关键安全约束。

说明：所有请求在模块级执行（不包外层 app_context），避免 Flask/Werkzeug 测试客户端在
同一 app_context 内多客户端登录 Cookie 相互干扰的框架级怪癖；DB 断言用短 app_context。
"""
import os
import sys
import io
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_smoke_tmp")
# 保证每次运行从干净状态开始，避免旧数据导致断言漂移
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "test.db").replace("\\", "/")
os.environ["STORAGE_ROOT"] = os.path.join(TMP, "storage")
os.environ["UPLOAD_TMP_ROOT"] = os.path.join(TMP, "uploads")

from app import create_app
from app.extensions import db
from app.models import User, File, ShareLink, MembershipPlan, AddonPackage

app = create_app()
CONTENT = b"HELLO WORLD 1234567890"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS" if cond else "FAIL"), name, extra)


def body(resp):
    return resp.data.decode("utf-8", "ignore")


def get_user(username):
    with app.app_context():
        return User.query.filter_by(username=username).first()


def get_file(user_id, name):
    with app.app_context():
        return File.query.filter_by(user_id=user_id, name=name, is_dir=False).first()


def first_file(user_id):
    with app.app_context():
        return File.query.filter_by(user_id=user_id, is_dir=False).first()


def last_share():
    with app.app_context():
        return ShareLink.query.order_by(ShareLink.id.desc()).first()


def count_trash(user_id):
    with app.app_context():
        return File.query.filter_by(user_id=user_id).filter(File.deleted_at.isnot(None)).count()


def reg(c, u):
    return c.post("/register", data={"username": u, "email": f"{u}@t.cn",
                                     "password": "pass1234", "confirm": "pass1234"},
                  follow_redirects=False)


def login(c, u):
    return c.post("/login", data={"username": u, "password": "pass1234"},
                  follow_redirects=False)


# ---- 准备：boss(管理员) + worker(普通) ----
boss = app.test_client()
worker = app.test_client()
reg(boss, "boss")
reg(worker, "worker")
with app.app_context():
    ub = User.query.filter_by(username="boss").first()
    ub.is_admin = True
    db.session.commit()
    check("注册+提权", ub.is_admin, f"boss id={ub.id}")
    ub_id = ub.id

# 非管理员访问 /admin/ 应 403（先登录 worker，验证通过后再登录 boss）
login(worker, "worker")
r = worker.get("/admin/")
check("非管理员403", r.status_code == 403, f"->{r.status_code}")
r = worker.get("/dashboard")
check("worker登录可用", r.status_code == 200, r.status_code)

login(boss, "boss")
r = boss.get("/admin/")
check("管理员访问后台", r.status_code == 200, f"->{r.status_code}")

# ---- 文件准备 ----
boss.post("/files/folder/new", data={"name": "Docs"})
with app.app_context():
    folder = File.query.filter_by(user_id=ub_id, name="Docs").first()
    check("建文件夹", folder is not None and folder.is_dir)
    folder_id = folder.id if folder else None


def up(c, filename, parent_id=None):
    data = {"parent_id": str(parent_id) if parent_id else "",
            "file": (io.BytesIO(CONTENT), filename)}
    return c.post("/files/upload", data=data, content_type="multipart/form-data")


up(boss, "doc.txt", folder_id)
up(boss, "hello2.bin")
with app.app_context():
    files = File.query.filter_by(user_id=ub_id, is_dir=False).all()
    doc = next(f for f in files if f.name == "doc.txt")
    bin2 = next(f for f in files if f.name == "hello2.bin")
    check("上传文件", len(files) >= 2, f"doc={doc.size}B bin={bin2.size}B")
    doc_id, bin2_id = doc.id, bin2.id

# ---- 用户端页面渲染 / 搜索 / 排序 / 目录 / 回收站 ----
for path, name in [("/dashboard", "dashboard页"), ("/files", "文件列表页"),
                   (f"/files/{folder_id}", "目录页"), ("/trash", "回收站页"),
                   ("/billing", "计费页"), ("/shares", "分享列表页"),
                   ("/files?q=doc", "搜索页"), ("/files?sort=size&order=desc", "排序页")]:
    r = boss.get(path)
    check(name, r.status_code == 200, f"{path}->{r.status_code}")

# 批删->回收站->清空
r = boss.post("/files/batch/delete", data={"ids": [str(doc_id), str(bin2_id)]})
check("批删", r.status_code in (302, 303))
check("回收站有内容", count_trash(ub_id) == 2, f"deleted={count_trash(ub_id)}")
r = boss.post("/trash/empty")
check("清空回收站", count_trash(ub_id) == 0, f"left={count_trash(ub_id)}")

# ---- 分享外链安全 ----
up(boss, "doc.txt")
with app.app_context():
    doc_id = File.query.filter_by(user_id=ub_id, name="doc.txt", is_dir=False).first().id
boss.post("/share/create", data={"file_id": str(doc_id), "expire_days": "7"})

def newest_token():
    with app.app_context():
        l = ShareLink.query.order_by(ShareLink.id.desc()).first()
        return l.token if l else None


def link_count(token):
    with app.app_context():
        l = ShareLink.query.filter_by(token=token).first()
        return l.download_count if l else -1


def create_share(file_id, password=None, days=None):
    data = {"file_id": str(file_id)}
    if password:
        data["password"] = password
    if days:
        data["expire_days"] = str(days)
    boss.post("/share/create", data=data)
    return newest_token()


link1 = create_share(doc_id, days=7)
check("创建免密分享", link1 is not None, f"token={link1}")

guest = app.test_client()
r = guest.get(f"/share/{link1}")
check("落地页(HTML非文件)", r.status_code == 200 and "download" in body(r), r.status_code)
r = guest.get(f"/share/{link1}/download")
check("GET直链被禁(405)", r.status_code == 405, f"got {r.status_code}")
r = guest.post(f"/share/{link1}/download")
check("POST网页下载", r.status_code == 200 and r.data == CONTENT, f"{r.status_code} len={len(r.data)}")
check("下载计数+1", link_count(link1) == 1, f"count={link_count(link1)}")

# 密码分享：5 次错误后锁定
link2 = create_share(doc_id, password="secret1")
for i in range(5):
    guest.post(f"/share/{link2}", data={"password": "wrong"})
r = guest.get(f"/share/{link2}")
check("错误5次后锁定提示", "错误次数过多" in body(r), r.status_code)
r = guest.post(f"/share/{link2}", data={"password": "secret1"})
check("锁定期间正确密码也拒", "错误次数过多" in body(r) or "密码错误" in body(r), r.status_code)

# 新密码分享：正确密码授权后可浏览+下载
link3 = create_share(doc_id, password="secret2")
g2 = app.test_client()
r = g2.get(f"/share/{link3}")
check("密码页", "password" in body(r).lower(), r.status_code)
r = g2.post(f"/share/{link3}", data={"password": "secret2"})
check("正确密码授权", r.status_code == 302 and "/share/" in r.headers.get("Location", ""), r.status_code)
r = g2.get(f"/share/{link3}")
check("授权后浏览", r.status_code == 200 and "download" in body(r), r.status_code)
r = g2.post(f"/share/{link3}/download")
check("授权后下载", r.status_code == 200 and r.data == CONTENT, r.status_code)

# ---- 管理端 ----
with app.app_context():
    wk_id = User.query.filter_by(username="worker").first().id
for p in ["/admin/", "/admin/plans", "/admin/addons", f"/admin/user/{ub_id}",
          f"/admin/user/{wk_id}"]:
    r = boss.get(p)
    check(f"管理页 {p}", r.status_code == 200, f"->{r.status_code}")

# 套餐/叠加包 新增
r = boss.post("/admin/plans/save", data={"name": "Pro", "display_name": "专业版",
                                         "monthly_price": "30", "storage_gb": "200",
                                         "max_file_mb": "2048", "speed_mbps": "50",
                                         "traffic_gb": "500", "allow_preview": "1",
                                         "sort": "3"})
with app.app_context():
    pro = MembershipPlan.query.filter_by(name="pro").first()
    pro_ok = pro is not None
check("新增套餐", pro_ok, r.status_code)
r = boss.post("/admin/addons/save", data={"type": "storage", "name": "测试容量包",
                                          "amount_gb": "50", "price": "20",
                                          "duration_days": "30", "sort": "99"})
with app.app_context():
    pkg = AddonPackage.query.filter_by(name="测试容量包").first()
    pkg_ok = pkg is not None
check("新增叠加包", pkg_ok, r.status_code)

boss.post(f"/admin/user/{wk_id}/balance", data={"delta": "100", "note": "smoke"})
with app.app_context():
    w = User.query.filter_by(username="worker").first()
    bal = str(w.balance)
    check("调余额", bal == "100.00", f"balance={bal}")
with app.app_context():
    vip_id = MembershipPlan.query.filter_by(name="vip").first().id
boss.post(f"/admin/user/{wk_id}/gift_plan", data={"plan_id": str(vip_id)})
with app.app_context():
    w = User.query.filter_by(username="worker").first()
    plan_ok = w.plan_id == vip_id
    check("赠送会员", plan_ok, f"plan={w.plan_id}")
boss.post(f"/admin/user/{ub_id}/toggle_admin")   # 自我保护：不能撤自己
with app.app_context():
    u = User.query.filter_by(username="boss").first()
    check("自撤保护", u.is_admin, "仍为管理员")
r = boss.get(f"/admin/user/{wk_id}")
check("用户详情(流水/记录)", r.status_code == 200, r.status_code)

print("\n===== SUMMARY =====")
fails = [x for x in results if not x[1]]
for n, ok, e in results:
    if not ok:
        print("  FAIL:", n, e)
print(f"total={len(results)} pass={len(results)-len(fails)} fail={len(fails)}")
sys.exit(1 if fails else 0)
