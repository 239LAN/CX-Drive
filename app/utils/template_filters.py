"""Jinja 模板过滤器"""
from app.utils.helpers import human_size, human_speed


def register_filters(app):
    app.jinja_env.filters["human_size"] = human_size
    app.jinja_env.filters["human_speed"] = human_speed
