#!/usr/bin/env bash
# ============================================================
# CX-Drive 创想云盘 Ubuntu 安装 / 覆盖更新脚本模板
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
#               均会保留，不会丢失
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
    if grep -q '^# CLOUDPAN_PAYLOAD_BEGIN$' "$0"; then
        echo ">> 使用内嵌源码包"
        PAYLOAD_DIR="$(mktemp -d)"
        sed -n '/^# CLOUDPAN_PAYLOAD_BEGIN$/,/^# CLOUDPAN_PAYLOAD_END$/p' "$0" \
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

# ---------- 系统依赖 ----------
echo ">> 安装系统依赖（python3 / venv / pip / rsync）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip rsync

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
    --exclude ".env" --exclude "*.db"
    --exclude "install.sh" --exclude "install-template.sh" --exclude "cloudpan-install.sh"
    --exclude "_smoke_run.py" --exclude "_smoke_tmp"
    --exclude ".pytest_cache" --exclude "*.log"
)
echo ">> 部署代码: $SRC -> $INSTALL_DIR"
rsync -a --delete "${EXCLUDES[@]}" "$SRC/" "$INSTALL_DIR/"

# ---------- 密钥与配置（仅首次生成，更新时保留） ----------
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

# ---------- systemd 服务 ----------
echo ">> 注册 systemd 服务 $SERVICE"
cat > "/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=CX-Drive (创想云盘) Web Service
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
    echo
    echo "===== ${MODE}完成 ====="
    echo "访问地址: http://${IP}:${PORT_VAL}"
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
# CLOUDPAN_PAYLOAD_BEGIN
# CLOUDPAN_PAYLOAD_END