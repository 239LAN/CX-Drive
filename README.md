# RealFiles

**简体中文 | [English](README.en.md)**

RealFiles 是一个开箱即用、可私有部署的轻量网盘系统。基于 Flask，支持文件管理、在线预览、压缩解压、分享外链、远程下载、回收站、会员套餐与管理员后台，一条命令即可在主流 Linux 服务器上完成部署或覆盖更新。

## 功能特性

- **文件管理**：文件夹目录、上传下载、搜索排序、重命名 / 移动 / 复制、批量操作、回收站防误删
- **在线预览**：图片、文本、音视频直接在网页中打开（`config.yml` 可开关，仅会员可用）
- **压缩与解压**：选中文件 / 文件夹一键打包为 zip，zip 包可在网页上直接解压，多选下载同样自动打包
- **大文件传输**：分片上传 + MD5 秒传，断点续传友好
- **分享外链**：单个文件或整个目录均可分享；支持提取密码、有效期、下载次数限制；访客无需登录即可浏览与下载（未登录下载限速 1 MiB/s），输入提取码后可一键转存到自己的网盘
- **远程下载**：填写文件 URL 交由服务器后台抓取入库（`config.yml` 可开关）
- **配额与会员**：容量、单文件大小、下载限速、月流量均可按套餐控制；支持叠加容量 / 流量包；已登录用户默认不限速
- **管理后台**：用户管理、套餐 / 叠加包管理、余额调整、会员赠送、统计面板
- **存储点管理**：支持多个存储位置（本地磁盘多挂载点 / FTP），每个存储点独立设置容量上限、达到 90% 视为已满，新文件按各点占用率均衡落盘；删除存储点可把数据均衡转移到其他存储点，空间不足时可选择删除数据（记录标记「已丢失」并在 7 天后清理）
- **安全设计**：密码哈希存储、上传扩展名白名单 + 文件头嗅探（拒收伪装成常见格式的可执行程序与脚本）、分享密码防暴力破解（错误限次锁定）、外链仅限网页下载杜绝直链盗刷
- **一键部署**：单文件安装包内嵌完整源码，Linux 上 `sudo bash RealFiles-release-<版本>-install.sh` 完成全新安装或覆盖更新
- **自动更新**：每天 0 点检查 GitHub Release，发现新版本自动覆盖更新（可在 `config.yml` 关闭，直连失败时回退公共代理）；页脚展示当前版本号与更新提示

## 技术栈

- Python 3 + Flask + SQLAlchemy + Flask-Login
- Bootstrap 5（CDN）+ Bootstrap Icons
- SQLite（开箱即用，可自行切换到 MySQL / PostgreSQL）
- 生产部署：Gunicorn + systemd（托管服务名 `realfiles`）

## 目录结构

```
RealFiles/
├── app.py                  # 开发入口（python app.py，端口 5000）
├── wsgi.py                 # 生产入口（gunicorn wsgi:app）
├── config.py               # 应用配置
├── config.yml              # 站点配置（功能开关 / HTTPS / 自动更新）
├── VERSION                 # 版本号（页脚展示与自动更新比对）
├── requirements.txt        # Python 依赖
├── gunicorn.conf.py        # Gunicorn 配置（端口默认 4280，可用 .env 覆盖）
├── manage.py               # 命令行：管理员增删/查询
├── app/
│   ├── __init__.py         # 应用工厂（创建表 + 初始化默认套餐）
│   ├── models.py           # 数据模型（用户/文件/分享/订单/流水…）
│   ├── scheduler.py        # 定时任务（每月流量重置、每日 0 点检查更新等）
│   ├── routes/             # 路由：auth / files / share / preview / billing / admin / main
│   ├── services/           # 业务服务：文件、配额、计费、远程下载、自动更新
│   ├── templates/          # Jinja2 模板
│   └── utils/              # 工具函数与 Jinja 过滤器
├── build_install.py        # 生成单文件安装包（安装脚本模板内嵌其中，输出到 dist/）
└── dist/                   # 构建产物：RealFiles-release-<版本>-install.sh
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

### 安装（单文件安装包）

将构建产物 `RealFiles-release-<版本>-install.sh` 上传到服务器后执行：

```bash
sudo bash RealFiles-release-<版本>-install.sh
```

脚本会自动完成：安装系统依赖 → 创建运行用户 → 部署代码到 `/opt/realfiles` → 生成密钥 `.env` → 创建虚拟环境并安装依赖 → 注册并启动 systemd 服务 `realfiles`。

> 源码与安装逻辑全部内嵌在这一个文件里，服务器上无需再上传其他文件。

### 覆盖更新

**再次执行同样的命令即可**。数据库（`instance/`）、用户数据（`storage/ uploads/`）、密钥（`.env`）会被自动保留，仅更新代码与依赖并重启服务。

> 从旧版（`/opt/cx-pan`，服务名 `cx-pan`）升级时无需手动处理：直接运行新版安装包，脚本会自动停用旧服务并迁移数据库、用户数据、`.env` 与 `config.yml` 到 `/opt/realfiles`。

### 首次使用

1. 在页面注册管理员账号
2. 提升为管理员：

```bash
sudo /opt/realfiles/venv/bin/python /opt/realfiles/manage.py create-admin <你的用户名>
```

### 常用运维命令

```bash
systemctl status realfiles                  # 查看服务状态
journalctl -u realfiles -f                  # 实时查看日志
sudo /opt/realfiles/venv/bin/python /opt/realfiles/manage.py list-admin      # 管理员列表
sudo /opt/realfiles/venv/bin/python /opt/realfiles/manage.py revoke-admin <用户名>  # 撤销管理员
```

### 自动更新

服务每天 0 点（服务器本地时间）请求 GitHub Releases API 比对版本：

1. 有新版本时下载发布脚本 `RealFiles-release-<版本>-install.sh`（与 Release 附件同名）到 `/opt/realfiles/instance/update/current.sh`，并校验 sha256；
2. 经安装脚本写入的 sudoers 规则（`/etc/sudoers.d/realfiles-update`，仅放行 `/usr/local/sbin/realfiles-auto-update`）以 root 在独立 systemd 单元中执行覆盖更新，避免重启服务时中断更新；
3. 数据库、用户数据、`.env`、`config.yml` 均保留；更新日志见 `/opt/realfiles/instance/update/update.log`。

直连 `github.com` / `api.github.com` 失败时（例如国内服务器），会自动回退到公共 GH 代理
`https://v4.gh-proxy.org/`，即把原地址拼在代理之后
（`https://v4.gh-proxy.org/https://github.com/.../RealFiles-release-<版本>-install.sh`）。
始终是「先直连，失败才走代理」，代理不可用时会记录错误并保留原有更新状态。

在 `config.yml` 中调整：

```yaml
update:
  enabled: true                          # false = 关闭自动安装，仍每天检查并在页脚提示新版本
  repo: 239LAN/RealFiles                 # 更新来源（GitHub 仓库 owner/name）
  proxy: https://v4.gh-proxy.org/        # 直连失败时的回退代理，留空则禁用
```

## 重新生成单文件安装包

修改源码后，如需更新发布用的安装包：

```bash
python build_install.py
# 读取根目录 VERSION，输出 dist/RealFiles-release-<版本>-install.sh
# 该文件既是服务器上的安装脚本，也直接作为 GitHub Release 附件上传（自动更新只认这个命名）
```

发布新版本前请先修改根目录 [VERSION](VERSION)（页脚与自动更新的版本比对均以此为准）。

## 配置项

部署后可通过 `/opt/realfiles/.env` 调整：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | 安装时随机生成 | 会话签名密钥，请勿外泄 |
| `REALFILES_BIND` | `0.0.0.0:4280` | 监听地址与端口 |
| `REALFILES_WORKERS` | 自动按 CPU 计算 | Gunicorn 进程数 |

> 改名前的旧前缀 `CLOUDPAN_BIND` / `CLOUDPAN_WORKERS` 仍可读取，仅用于兼容已有部署；新部署请使用 `REALFILES_` 前缀。

## 说明

- 充值功能请自行对接支付API。
- 前端依赖（Bootstrap）通过公共 CDN 加载；若需完全内网环境，请自行下载静态资源并修改 `app/templates/base.html`。
- 不建议用于生产环境。

## License

RealFiles © 2026 [239LAN](https://github.com/239LAN)，采用 **GNU Affero General Public License v3.0** 开源，详见 [LICENSE](LICENSE)。

按 AGPL-3.0 要求：基于本项目提供网络服务（含二次开发、修改后部署）的一方，也必须以相同协议向服务使用者开放其完整源代码。
