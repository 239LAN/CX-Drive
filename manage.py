# -*- coding: utf-8 -*-
"""管理员账号管理命令行工具

用法：
    python manage.py list-admins               查看全部管理员
    python manage.py create-admin <username>    将某用户提升为管理员
    python manage.py revoke-admin <username>    撤销某用户的管理员身份
"""
import sys


def _usage():
    print(__doc__)
    sys.exit(1)


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("list-admins", "create-admin", "revoke-admin"):
        _usage()

    from app import create_app, db
    from app.models import User

    app = create_app()
    with app.app_context():
        if args[0] == "list-admins":
            admins = User.query.filter_by(is_admin=True).all()
            if not admins:
                print("当前没有管理员账号。")
            for u in admins:
                print(f"- {u.username} (id={u.id})")
            return

        username = args[1] if len(args) > 1 else ""
        u = User.query.filter_by(username=username).first()
        if u is None:
            print(f"用户不存在：{username}")
            sys.exit(1)

        if args[0] == "create-admin":
            if u.is_admin:
                print(f"{username} 已是管理员。")
            else:
                u.is_admin = True
                db.session.commit()
                print(f"已将 {username} 提升为管理员。")
        else:  # revoke-admin
            if not u.is_admin:
                print(f"{username} 不是管理员。")
            elif User.query.filter_by(is_admin=True).count() <= 1:
                print("系统至少需要保留一名管理员，无法撤销。")
            else:
                u.is_admin = False
                db.session.commit()
                print(f"已撤销 {username} 的管理员身份。")


if __name__ == "__main__":
    main()
