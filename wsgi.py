"""WSGI 入口（gunicorn 生产环境使用）

注意：项目根目录存在 app 包（app/），因此 gunicorn 不能使用 "app:app" 作为入口，
否则会导入 app 包而不是 app.py，本文件是标准的 WSGI 入口。
"""
from app import create_app

app = create_app()
