# CX-Drive · 创想云盘

CX-Drive（创想云盘）是一个开箱即用、可私有部署的轻量网盘系统。基于 Flask，支持文件管理、分享外链、回收站、会员套餐与管理员后台，一条命令即可在主流 Linux 服务器上完成部署或覆盖更新。

## 功能特性

- **文件管理**：文件夹目录、上传下载、搜索排序、重命名 / 移动 / 复制、批量操作、回收站防误删
- **大文件传输**：分片上传 + MD5 秒传，断点续传友好
- **分享外链**：单个文件或整个目录均可分享；支持提取密码、有效期、下载次数限制；访客无需登录即可浏览与下载（未登录下载限速 1 MiB/s）
- **配额与会员**：容量、单文件大小、下载限速、月流量均可按套餐控制；支持叠加容量 / 流量包
- **管理后台**：用户管理、套餐 / 叠加包管理、余额调整、会员赠送、统计面板
- **安全设计**：密码哈希存储、分享密码防暴力破解（错误限次锁定）、外链仅限网页下载杜绝直链盗刷
- **一键部署**：单文件安装包内嵌完整源码，Ubuntu 上 `sudo bash install.sh` 完成全新安装或覆盖更新

## 技术栈

- Python 3 + Flask + SQLAlchemy + Flask-Login
- Bootstrap 5（CDN）+ Bootstrap Icons
- SQLite（开箱即用，可自行切换到 MySQL / PostgreSQL）
- 生产部署：Gunicorn + systemd（托管服务名 `cx-pan`）

## 目录结构

```
创想云盘/
├── app.py                  # 开发入口（python app.py，端口 5000）
├── wsgi.py                 # 生产入口（gunicorn wsgi:app）
├── config.py               # 应用配置
├── requirements.txt        # Python 依赖
├── gunicorn.conf.py        # Gunicorn 配置（端口默认 4280，可用 .env 覆盖）
├── manage.py               # 命令行：管理员增删/查询
├── app/
│   ├── __init__.py         # 应用工厂（创建表 + 初始化默认套餐）
│   ├── models.py           # 数据模型（用户/文件/分享/订单/流水…）
│   ├── scheduler.py        # 定时任务（回收站清理等）
│   ├── routes/             # 路由：auth / files / share / preview / billing / admin / main
│   ├── services/           # 业务服务：文件、配额、计费
│   ├── templates/          # Jinja2 模板
│   └── utils/              # 工具函数与 Jinja 过滤器
├── build_install.py        # 生成单文件安装包（输出 install.sh）
├── install-template.sh     # 安装脚本模板（构建器用）
├── install.sh              # 单文件安装包（构建产物，可直接部署）
└── _smoke_run.py           # 回归冒烟测试
```

## 本地开发

需要 Python 3.10+。

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python app.py            # 浏览器打开 http://127.0.0.1:5000
```

## 部署到 Linux（systemd 发行版）

支持自动识别包管理器并安装依赖，覆盖以下主流发行版：

| 发行版 | 包管理器 | 系统依赖 |
| --- | --- | --- |
| Debian / Ubuntu | apt | python3 + venv + pip + rsync |
| RHEL / CentOS Stream / AlmaLinux / Rocky / Fedora | dnf（旧版 yum） | python3 + pip + rsync |
| Arch Linux | pacman | python + pip + rsync |
| openSUSE | zypper | python3 + pip + rsync |

> Alpine（apk / OpenRC）暂不支持——脚本依赖 systemd 托管服务；若必须使用，可手动改用 rc-service 或容器方式。

### 方式一：单文件安装包（推荐）

将构建产物 `install.sh` 上传到服务器后执行：

```bash
sudo bash install.sh
```

脚本会自动完成：安装系统依赖 → 创建运行用户 → 部署代码到 `/opt/cx-pan` → 生成密钥 `.env` → 创建虚拟环境并安装依赖 → 注册并启动 systemd 服务 `cx-pan`。

### 方式二：源码目录直接安装

把项目源码放到服务器任意目录，执行：

```bash
sudo bash install-template.sh            # 脚本与源码同目录
sudo bash install-template.sh /path/to/源码目录
```

### 覆盖更新

**再次执行同样的命令即可**。数据库（`instance/`）、用户数据（`storage/ uploads/`）、密钥（`.env`）会被自动保留，仅更新代码与依赖并重启服务。

### 首次使用

1. 在页面注册管理员账号
2. 提升为管理员：

```bash
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py create-admin <你的用户名>
```

### 常用运维命令

```bash
systemctl status cx-pan                  # 查看服务状态
journalctl -u cx-pan -f                  # 实时查看日志
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py list-admin      # 管理员列表
sudo /opt/cx-pan/venv/bin/python /opt/cx-pan/manage.py revoke-admin <用户名>  # 撤销管理员
```

## 重新生成单文件安装包

修改源码后，如需更新发布用的安装包：

```bash
python build_install.py
# 输出 install.sh，负载已包含最新源码（数据/密钥/开发残留自动排除）
```

## 冒烟测试

```bash
python _smoke_run.py
```

使用隔离的临时数据库与存储目录，覆盖用户端 / 分享外链 / 管理端共 39 项回归断言，全部通过即基础功能健康。

## 配置项

部署后可通过 `/opt/cx-pan/.env` 调整：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | 安装时随机生成 | 会话签名密钥，请勿外泄 |
| `CLOUDPAN_BIND` | `0.0.0.0:4280` | 监听地址与端口 |
| `CLOUDPAN_WORKERS` | 自动按 CPU 计算 | Gunicorn 进程数 |

## 说明

- 充值功能暂未开放，用户余额仅由管理员在后台调整。
- 前端依赖（Bootstrap）通过公共 CDN 加载；若需完全内网环境，请自行下载静态资源并修改 `app/templates/base.html`。

## License

CX-Drive（创想云盘）© 2026 [239LAN](https://github.com/239LAN)，采用 **GNU Affero General Public License v3.0** 开源，详见 [LICENSE](LICENSE)。

按 AGPL-3.0 要求：基于本项目提供网络服务（含二次开发、修改后部署）的一方，也必须以相同协议向服务使用者开放其完整源代码。
