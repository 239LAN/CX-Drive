"""Jinja 模板过滤器"""
from app.utils.helpers import human_duration, human_size, human_speed, split_duration


def register_filters(app):
    app.jinja_env.filters["human_size"] = human_size
    app.jinja_env.filters["human_speed"] = human_speed
    app.jinja_env.filters["human_duration"] = human_duration
    # 表单回填「时长数值 + 单位」时需要拆分秒数
    app.jinja_env.globals["split_duration"] = split_duration
