#!/usr/bin/env bash
# ============================================================
# RealFiles Linux 安装 / 覆盖更新脚本模板
#
# 用法（需 root 或 sudo）：
#   sudo bash RealFiles-release-<版本>-install.sh   # 单文件安装包（内嵌完整源码）
#   sudo bash install-template.sh          # 模板与源码同目录时直接执行
#   sudo bash install-template.sh /path/to/源码目录
#   sudo bash install-template.sh /path/to/app.tar.gz # 源码压缩包
#
# 说明：
#   本文件是「安装脚本模板」。运行 build_install.py 会把整个项目
#   源码内嵌到本文件尾部的负载区，生成单文件安装包
#   RealFiles-release-<版本>-install.sh（版本号取自根目录 VERSION），
#   在目标机器上单独拷贝该文件执行即可完成部署。
#
# 特性：
#   - 全新安装：部署全部文件、创建虚拟环境、安装依赖、写入 .env 密钥、
#               注册 systemd 服务并启动
#   - 覆盖更新：再次执行即更新代码与依赖并重启服务；数据库
#               (instance/)、用户数据 (storage/ uploads/)、密钥 (.env)
#               与站点配置 (config.yml) 均会保留，不会丢失
#   - HTTPS/HSTS：由 config.yml 的 https 段控制开关与证书路径；
#               证书缺失时自动回退为 HTTP，HSTS 随之关闭
#   - 自动更新：应用按 config.yml 的 update 段定时（默认 0 点）检查 GitHub Release，
#               发现新版本时下载发布脚本，经 sudoers 放行的入口以 root 覆盖更新；
#               关闭自动安装后仍会检查并在页脚提示，管理后台可手动检查/更新
#   - 服务重启：管理后台「设置」页可经 sudoers 放行的入口重启本服务
# ============================================================
set -euo pipefail

APP_NAME="realfiles"
INSTALL_DIR="/opt/realfiles"
RUN_USER="realfiles"
SERVICE="${APP_NAME}.service"
BIND_DEFAULT="0.0.0.0:4280"

# 改名前的旧安装（CX-Drive / cx-pan），存在时自动迁移
LEGACY_APP_NAME="cx-pan"
LEGACY_DIR="/opt/${LEGACY_APP_NAME}"

# ---------- 参数与源码解析 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:-${SCRIPT_DIR}}"
TMP_EXTRACT=""

cleanup() { [ -n "$TMP_EXTRACT" ] && rm -rf "$TMP_EXTRACT"; }
trap cleanup EXIT

resolve_src() {
    # 单文件安装包：优先使用脚本内嵌的源码负载（由 build_install.py 生成）
    if grep -q '^# REALFILES_BEGIN$' "$0"; then
        echo ">> 使用内嵌源码包"
        PAYLOAD_DIR="$(mktemp -d)"
        sed -n '/^# REALFILES_BEGIN$/,/^# REALFILES_END$/p' "$0" \
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

# 旧版（改名前的 cx-pan）安装检测：存在则本次按「覆盖更新 + 迁移」处理
LEGACY_FOUND=0
if [ "$LEGACY_DIR" != "$INSTALL_DIR" ] && [ -f "$LEGACY_DIR/.env" ]; then
    LEGACY_FOUND=1
fi

MODE="全新安装"
[ -f "$INSTALL_DIR/.env" ] && MODE="覆盖更新"
if [ "$MODE" = "全新安装" ] && [ "$LEGACY_FOUND" = "1" ]; then
    MODE="覆盖更新（自 ${LEGACY_DIR} 迁移）"
fi

echo "===== RealFiles 部署：${MODE} ====="
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
mkdir -p "$INSTALL_DIR/instance" "$INSTALL_DIR/storage" "$INSTALL_DIR/uploads" "$INSTALL_DIR/storage_cache"

# ---------- 旧版安装迁移（改名前的 cx-pan -> realfiles） ----------
# 应用由 CX-Drive 更名为 RealFiles 后，安装目录与服务名同步变更。若检测到旧的
# /opt/cx-pan 安装，则停用旧服务、清理旧入口，并把数据/密钥/站点配置迁移过来。
if [ "$LEGACY_FOUND" = "1" ]; then
    echo ">> 检测到旧版安装 ${LEGACY_DIR}，开始迁移到 ${INSTALL_DIR}"

    # 1) 停用并移除旧服务
    if [ -f "/etc/systemd/system/${LEGACY_APP_NAME}.service" ]; then
        systemctl stop "${LEGACY_APP_NAME}.service" 2>/dev/null || true
        systemctl disable "${LEGACY_APP_NAME}.service" 2>/dev/null || true
        rm -f "/etc/systemd/system/${LEGACY_APP_NAME}.service"
        echo ">> 已停止并移除旧服务 ${LEGACY_APP_NAME}.service"
    fi

    # 2) 清理旧 sudoers 规则与执行入口（新版会重新生成 realfiles-* 入口）
    rm -f /etc/sudoers.d/cx-pan-update
    rm -f /usr/local/sbin/cx-pan-auto-update /usr/local/sbin/cx-pan-restart

    # 3) 迁移数据目录与密钥/站点配置（目标已存在则不覆盖，避免破坏已有数据）
    for _d in instance storage uploads storage_cache; do
        if [ -d "$LEGACY_DIR/$_d" ] && [ ! -e "$INSTALL_DIR/$_d" ]; then
            mv "$LEGACY_DIR/$_d" "$INSTALL_DIR/$_d"
            echo ">> 已迁移目录 $_d"
        fi
    done
    for _f in .env config.yml; do
        if [ -f "$LEGACY_DIR/$_f" ] && [ ! -e "$INSTALL_DIR/$_f" ]; then
            mv "$LEGACY_DIR/$_f" "$INSTALL_DIR/$_f"
            echo ">> 已迁移文件 $_f"
        fi
    done

    # 4) 旧版 SQLite 库文件名兼容（cloud_drive.db -> realfiles.db）
    if [ ! -f "$INSTALL_DIR/instance/realfiles.db" ]; then
        for _db in cloud_drive.db cloudpan.db cx_pan.db; do
            if [ -f "$INSTALL_DIR/instance/$_db" ]; then
                mv "$INSTALL_DIR/instance/$_db" "$INSTALL_DIR/instance/realfiles.db"
                echo ">> 已迁移数据库 $_db -> realfiles.db"
                break
            fi
        done
    fi

    # 5) 旧 .env 内的 CLOUDPAN_* 前缀统一改写为 REALFILES_*
    #    应用仍兼容读取旧前缀，此处改写只为保持配置项命名一致
    if [ -f "$INSTALL_DIR/.env" ] && grep -q '^CLOUDPAN_' "$INSTALL_DIR/.env"; then
        sed -i 's/^CLOUDPAN_/REALFILES_/' "$INSTALL_DIR/.env"
        echo ">> 已将 .env 中的 CLOUDPAN_* 前缀改写为 REALFILES_*"
    fi

    echo ">> 迁移完成（旧目录 ${LEGACY_DIR} 保留未删除，确认无误后可自行清理）"
fi

# ---------- 部署代码（覆盖更新：排除数据/密钥/环境以保留它们） ----------
EXCLUDES=(
    --exclude ".git" --exclude ".gitignore"
    --exclude "__pycache__" --exclude "*.pyc"
    --exclude "venv" --exclude ".venv"
    --exclude "instance" --exclude "storage" --exclude "uploads" --exclude "storage_cache"
    --exclude ".env" --exclude "config.yml" --exclude "*.db"
    --exclude "install.sh" --exclude "install-template.sh" --exclude "realfiles-install.sh"
    --exclude "cloudpan-install.sh"
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
# RealFiles 生产配置（请勿提交到版本库 / 覆盖更新时保留）
SECRET_KEY=${SECRET}
REALFILES_BIND=${REALFILES_BIND:-${BIND_DEFAULT}}
# REALFILES_WORKERS=4
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
Description=RealFiles Web Service
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
# 应用发现新版本后把发布脚本下载到 instance/update/current.sh，再以 realfiles 身份
# 执行下面的 wrapper（sudoers 仅放行这一条命令）；wrapper 通过 systemd-run 起独立
# 单元，脱离 realfiles.service 的 cgroup，避免更新脚本重启服务时自身被连带杀掉。
echo ">> 配置自动更新执行入口"
mkdir -p "$INSTALL_DIR/instance/update"
cat > /usr/local/sbin/realfiles-auto-update <<EOF
#!/usr/bin/env bash
# RealFiles 自动更新执行入口（由安装脚本生成，请勿手工修改）
# 用法：realfiles-auto-update      由应用经 sudo 调用，执行 $INSTALL_DIR/instance/update/current.sh
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR}"
UPDATE_DIR="\${INSTALL_DIR}/instance/update"
SCRIPT="\${UPDATE_DIR}/current.sh"
LOG="\${UPDATE_DIR}/update.log"

if [ "\${1:-}" != "--run" ]; then
    SR="\$(command -v systemd-run || true)"
    if [ -n "\$SR" ]; then
        exec "\$SR" --unit=realfiles-update --collect --no-block \\
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
chown root:root /usr/local/sbin/realfiles-auto-update
chmod 750 /usr/local/sbin/realfiles-auto-update

# ---------- 服务重启执行入口（管理后台触发 + sudoers） ----------
# 管理后台「设置」页的「重启服务」按钮：以 realfiles 身份执行下面的 wrapper，
# wrapper 通过 systemd-run 起独立单元重启 realfiles.service，避免重启过程中
# 把自己所在的单元一起杀掉导致中断。
echo ">> 配置服务重启执行入口"
cat > /usr/local/sbin/realfiles-restart <<EOF
#!/usr/bin/env bash
# RealFiles 服务重启入口（由安装脚本生成，请勿手工修改）
# 用法：realfiles-restart      由应用经 sudo 调用，重启 ${APP_NAME}.service
set -euo pipefail

if [ "\${1:-}" != "--run" ]; then
    SR="\$(command -v systemd-run || true)"
    if [ -n "\$SR" ]; then
        exec "\$SR" --unit=realfiles-restart --collect --no-block \\
            --property=Type=oneshot --property=RemainAfterExit=no "\$0" --run
    fi
    exec "\$0" --run
fi

sleep 1
systemctl restart ${APP_NAME}.service
EOF
chown root:root /usr/local/sbin/realfiles-restart
chmod 750 /usr/local/sbin/realfiles-restart

cat > /etc/sudoers.d/realfiles-update <<EOF
# RealFiles：仅允许 ${RUN_USER} 免密执行更新/重启入口（且不带参数）
${RUN_USER} ALL=(root) NOPASSWD: /usr/local/sbin/realfiles-auto-update ""
${RUN_USER} ALL=(root) NOPASSWD: /usr/local/sbin/realfiles-restart ""
EOF
chmod 440 /etc/sudoers.d/realfiles-update
if command -v visudo >/dev/null 2>&1; then
    if ! visudo -cf /etc/sudoers.d/realfiles-update >/dev/null; then
        rm -f /etc/sudoers.d/realfiles-update
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
    BIND_VAL="$(awk -F= '/^REALFILES_BIND=/{v=$2} /^CLOUDPAN_BIND=/{if(v=="")v=$2} END{print v}' "$INSTALL_DIR/.env")"
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
# REALFILES_BEGIN
# REALFILES_END