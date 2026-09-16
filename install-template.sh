#!/usr/bin/env bash
# ============================================================
# 创想云盘 Linux 安装 / 覆盖更新脚本模板
#
# 用法（需 root 或 sudo）：
#   sudo bash install.sh                   # 单文件安装包（内嵌完整源码）
#   sudo bash install-template.sh          # 模板与源码同目录时直接执行
#   sudo bash install-template.sh /path/to/源码目录
#   sudo bash install-template.sh /path/to/app.tar.gz # 源码压缩包
#
# 说明：
#   本文件是「安装脚本模板」。运行 build_install.py 会把整个项目
#   源码内嵌到本文件尾部的负载区，生成单文件安装包 install.sh，
#   在目标 Ubuntu 机器上单独拷贝 install.sh 执行即可完成部署。
#
# 特性：
#   - 全新安装：部署全部文件、创建虚拟环境、安装依赖、写入 .env 密钥、
#               注册 systemd 服务并启动
#   - 覆盖更新：再次执行即更新代码与依赖并重启服务；数据库
#               (instance/)、用户数据 (storage/ uploads/)、密钥 (.env)
#               与站点配置 (config.yml) 均会保留，不会丢失
#   - HTTPS/HSTS：由 config.yml 的 https 段控制开关与证书路径；
#               证书缺失时自动回退为 HTTP，HSTS 随之关闭
#   - 自动更新：应用每天 0 点检查 GitHub Release（config.yml 的 update 段可关闭），
#               发现新版本时下载发布脚本，经 sudoers 放行的入口以 root 覆盖更新
# ============================================================
set -euo pipefail

APP_NAME="cx-pan"
INSTALL_DIR="/opt/cx-pan"
RUN_USER="cx-pan"
SERVICE="${APP_NAME}.service"
BIND_DEFAULT="0.0.0.0:4280"

# ---------- 参数与源码解析 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:-${SCRIPT_DIR}}"
TMP_EXTRACT=""

cleanup() { [ -n "$TMP_EXTRACT" ] && rm -rf "$TMP_EXTRACT"; }
trap cleanup EXIT

resolve_src() {
    # 单文件安装包：优先使用脚本内嵌的源码负载（由 build_install.py 生成）
    if grep -q '^# CX_DRIVE_BEGIN$' "$0"; then
        echo ">> 使用内嵌源码包"
        PAYLOAD_DIR="$(mktemp -d)"
        sed -n '/^# CX_DRIVE_BEGIN$/,/^# CX_DRIVE_END$/p' "$0" \
            | sed '1d;$d' \
            | base64 -d | tar -xzf - -C "$PAYLOAD_DIR"
        SRC="$PAYLOAD_DIR"
        TMP_EXTRACT="$PAYLOAD_DIR"
        return
    fi
    # 支持 .tar.gz / .tgz 压缩包作为源码
    if [ -f "$SRC" ] && [[ "$SRC" == *.tar.gz || "$SRC" == *.tgz ]]; then
        echo ">> 解压源码包: $SRC"
        TMP_EXTRACT="$(mktemp -d)"
        tar -xzf "$SRC" -C "$TMP_EXTRACT"
        SRC="$TMP_EXTRACT"
        local first_dir
        first_dir="$(find "$TMP_EXTRACT" -mindepth 1 -maxdepth 1 -type d | head -n1)"
        if [ -n "$first_dir" ] && [ "$(find "$TMP_EXTRACT" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ]; then
            SRC="$first_dir"   # 压缩包内仅一个目录时取其内容
        fi
    fi
    if [ ! -d "$SRC" ] || [ ! -f "$SRC/requirements.txt" ]; then
        echo "错误：源码目录无效（缺少 requirements.txt）：$SRC" >&2
        exit 1
    fi
    SRC="$(cd "$SRC" && pwd)"
}

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 或 sudo 运行本脚本。" >&2
    exit 1
fi
resolve_src

MODE="全新安装"
[ -f "$INSTALL_DIR/.env" ] && MODE="覆盖更新"

echo "===== 创想云盘部署：${MODE} ====="
echo "源码目录: $SRC"
echo "安装目录: $INSTALL_DIR"

# ---------- 系统依赖（兼容主流 Linux 发行版） ----------
# 支持的包管理器：apt(Debian/Ubuntu) / dnf、yum(RHEL/CentOS/Fedora/Alma/Rocky)
#                / pacman(Arch) / zypper(openSUSE)。Alpine(apk, OpenRC) 不支持。
install_sys_pkgs() {
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            python3 python3-venv python3-pip rsync
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip rsync
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-pip rsync
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm python python-pip rsync
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 python3-pip rsync
    elif command -v apk >/dev/null 2>&1; then
        echo "错误：Alpine(apk) 使用 OpenRC 而非 systemd，本脚本暂不支持。" >&2
        echo "      请使用 Debian/Ubuntu、RHEL/CentOS/Fedora、Arch、openSUSE 等发行版。" >&2
        exit 1
    else
        echo "错误：未识别的包管理器，无法自动安装依赖。" >&2
        exit 1
    fi
}
echo ">> 安装系统依赖（python3 / venv / pip / rsync）"
install_sys_pkgs

# ---------- 运行用户与数据目录 ----------
if ! id -u "$RUN_USER" &>/dev/null; then
    useradd --system --no-create-home --home-dir "$INSTALL_DIR" \
        --shell /usr/sbin/nologin "$RUN_USER"
    echo ">> 已创建系统用户 $RUN_USER"
fi
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/instance" "$INSTALL_DIR/storage" "$INSTALL_DIR/uploads"

# ---------- 部署代码（覆盖更新：排除数据/密钥/环境以保留它们） ----------
EXCLUDES=(
    --exclude ".git" --exclude ".gitignore"
    --exclude "__pycache__" --exclude "*.pyc"
    --exclude "venv" --exclude ".venv"
    --exclude "instance" --exclude "storage" --exclude "uploads"
    --exclude ".env" --exclude "config.yml" --exclude "*.db"
    --exclude "install.sh" --exclude "install-template.sh" --exclude "cloudpan-install.sh"
    --exclude "dist"
    --exclude "_smoke_run.py" --exclude "_smoke_tmp"
    --exclude "_chk_new.py" --exclude "_chk_tmp"
    --exclude "_rt_check.py" --exclude "_rt_tmp"
    --exclude ".pytest_cache" --exclude "*.log"
)
echo ">> 部署代码: $SRC -> $INSTALL_DIR"
rsync -a --delete "${EXCLUDES[@]}" "$SRC/" "$INSTALL_DIR/"

# ---------- 密钥与配置（仅首次生成，更新时保留） ----------
# 站点配置：首次安装从源码复制一份，之后用户的自定义修改不会被覆盖
if [ ! -f "$INSTALL_DIR/config.yml" ]; then
    cp "$SRC/config.yml" "$INSTALL_DIR/config.yml"
    echo ">> 已生成站点配置 config.yml（网站名与功能开关）"
fi

if [ ! -f "$INSTALL_DIR/.env" ]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$INSTALL_DIR/.env" <<EOF
# 创想云盘生产配置（请勿提交到版本库 / 覆盖更新时保留）
SECRET_KEY=${SECRET}
CLOUDPAN_BIND=${CLOUDPAN_BIND:-${BIND_DEFAULT}}
# CLOUDPAN_WORKERS=4
EOF
    chmod 600 "$INSTALL_DIR/.env"
    echo ">> 已生成密钥配置 .env"
fi

# ---------- 虚拟环境与依赖 ----------
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
    echo ">> 已创建虚拟环境"
fi
echo ">> 安装/更新 Python 依赖"
"$INSTALL_DIR/venv/bin/python" -m pip install --upgrade -q pip wheel setuptools
"$INSTALL_DIR/venv/bin/python" -m pip install -q -r "$INSTALL_DIR/requirements.txt"

# ---------- 源码编译为字节码（提升启动速度）并移除 .py 源码 ----------
# 用服务器 Python 将源码实时编译为与源码同目录的 .pyc（legacy sourceless 布局：
# Python 导入器在缺少 .py 时会加载同目录的 .pyc），随后删除 .py，启动/导入直接命中字节码。
# gunicorn.conf.py（按文件路径解析）、manage.py / app.py（按脚本方式执行）必须保留源码。
echo ">> 清理旧字节码并编译新字节码（.py -> .pyc）..."
find "$INSTALL_DIR" -name "__pycache__" -type d -not -path "*/venv/*" -prune -exec rm -rf {} +
find "$INSTALL_DIR" -name "*.pyc" -not -path "*/venv/*" -delete
COMPILE_OK=1
while IFS= read -r src; do
    if ! "$INSTALL_DIR/venv/bin/python" -c "import py_compile,sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)" "$src" "${src%.py}.pyc"; then
        COMPILE_OK=0
        break
    fi
done < <(find "$INSTALL_DIR" -name "*.py" -not -path "*/venv/*" \
            -not -name "manage.py" -not -name "gunicorn.conf.py" -not -name "app.py")
if [ "$COMPILE_OK" = "1" ]; then
    echo ">> 移除 .py 源码（保留入口 manage.py / gunicorn.conf.py / app.py）"
    find "$INSTALL_DIR" -name "*.py" -not -path "*/venv/*" \
        -not -name "manage.py" -not -name "gunicorn.conf.py" -not -name "app.py" -delete
else
    echo "警告：字节码编译未完全成功，保留源码运行（功能不受影响）"
    find "$INSTALL_DIR" -name "*.pyc" -not -path "*/venv/*" -delete
fi

# ---------- systemd 服务 ----------
echo ">> 注册 systemd 服务 $SERVICE"
cat > "/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=创想云盘 Web Service
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ---------- 自动更新执行入口（应用内触发 + sudoers） ----------
# 应用发现新版本后把发布脚本下载到 instance/update/current.sh，再以 cx-pan 身份
# 执行下面的 wrapper（sudoers 仅放行这一条命令）；wrapper 通过 systemd-run 起独立
# 单元，脱离 cx-pan.service 的 cgroup，避免更新脚本重启服务时自身被连带杀掉。
echo ">> 配置自动更新执行入口"
mkdir -p "$INSTALL_DIR/instance/update"
cat > /usr/local/sbin/cx-pan-auto-update <<EOF
#!/usr/bin/env bash
# 创想云盘自动更新执行入口（由安装脚本生成，请勿手工修改）
# 用法：cx-pan-auto-update      由应用经 sudo 调用，执行 $INSTALL_DIR/instance/update/current.sh
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR}"
UPDATE_DIR="\${INSTALL_DIR}/instance/update"
SCRIPT="\${UPDATE_DIR}/current.sh"
LOG="\${UPDATE_DIR}/update.log"

if [ "\${1:-}" != "--run" ]; then
    SR="\$(command -v systemd-run || true)"
    if [ -n "\$SR" ]; then
        exec "\$SR" --unit=cx-pan-update --collect --no-block \\
            --property=Type=oneshot --property=RemainAfterExit=no "\$0" --run
    fi
    exec "\$0" --run
fi

mkdir -p "\$UPDATE_DIR"
{
    echo "===== \$(date '+%F %T') 自动更新开始 ====="
    if [ ! -f "\$SCRIPT" ]; then
        echo "未找到待执行的更新脚本：\$SCRIPT"
        exit 1
    fi
    bash "\$SCRIPT"
    echo "===== \$(date '+%F %T') 自动更新结束 ====="
} >> "\$LOG" 2>&1
rm -f "\$SCRIPT"
EOF
chown root:root /usr/local/sbin/cx-pan-auto-update
chmod 750 /usr/local/sbin/cx-pan-auto-update

cat > /etc/sudoers.d/cx-pan-update <<EOF
# 创想云盘自动更新：仅允许 ${RUN_USER} 免密执行更新入口（且不带参数）
${RUN_USER} ALL=(root) NOPASSWD: /usr/local/sbin/cx-pan-auto-update ""
EOF
chmod 440 /etc/sudoers.d/cx-pan-update
if command -v visudo >/dev/null 2>&1; then
    if ! visudo -cf /etc/sudoers.d/cx-pan-update >/dev/null; then
        rm -f /etc/sudoers.d/cx-pan-update
        echo "警告：sudoers 规则校验失败，已移除；自动更新将无法安装（仍会每日检查并提示）"
    fi
fi
command -v sudo >/dev/null 2>&1 || \
    echo "警告：未检测到 sudo，自动更新将无法安装（仍会每日检查并在页脚提示）"

chown -R "${RUN_USER}:${RUN_USER}" "$INSTALL_DIR"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 2

# ---------- 结果 ----------
if systemctl is-active --quiet "$SERVICE"; then
    BIND_VAL="$(awk -F= '/^CLOUDPAN_BIND=/{v=$2} END{print v}' "$INSTALL_DIR/.env")"
    PORT_VAL="${BIND_VAL##*:}"
    [ -z "$PORT_VAL" ] || [ "$PORT_VAL" = "$BIND_VAL" ] && PORT_VAL="4280"
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    # 按 config.yml 的实际生效结果给出访问协议（证书缺失时已自动回退 HTTP）
    SCHEME="http"
    if "$INSTALL_DIR/venv/bin/python" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from config import load_site_config; raise SystemExit(0 if load_site_config()['HTTPS_ENABLED'] else 1)" 2>/dev/null; then
        SCHEME="https"
    fi
    echo
    echo "===== ${MODE}完成 ====="
    echo "访问地址: ${SCHEME}://${IP}:${PORT_VAL}"
    echo "服务状态: systemctl status ${SERVICE}"
    echo "实时日志: journalctl -u ${SERVICE} -f"
    echo "提升管理员: 先在页面注册账号，然后执行"
    echo "  sudo ${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/manage.py create-admin <用户名>"
else
    echo "===== 服务启动失败，最近日志如下 =====" >&2
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    exit 1
fi

# ============================================================
# 以下为内嵌源码包负载，由 build_install.py 生成，请勿手工编辑
# ============================================================
exit 0
# CX_DRIVE_BEGIN
# CX_DRIVE_END