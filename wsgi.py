"""WSGI 入口（gunicorn 生产环境使用）

根目录存在 app/ 包，若以 "app:app" 为入口会导入 app 包而非 app.py，故 gunicorn 使用本文件。
"""
from app import create_app

app = create_app()
