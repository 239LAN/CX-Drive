"""扩展实例（db / login_manager / bcrypt）"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_bcrypt import Bcrypt

db = SQLAlchemy()
login_manager = LoginManager()
bcrypt = Bcrypt()

login_manager.login_view = "auth.login"
login_manager.login_message = "请先登录"
login_manager.login_message_category = "warning"
