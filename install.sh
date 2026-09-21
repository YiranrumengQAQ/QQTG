#!/usr/bin/env bash
# ============================================================================
#  Rain Bridge - 群组桥接（Telegram 内置，未来平台可插拔） 一键安装 / 更新 / 卸载脚本
#
#  安装:
#    bash <(curl -fsSL https://raw.githubusercontent.com/YiranrumengQAQ/QQTG/main/install.sh)
#
#  常用:
#    bash install.sh                      交互式安装（已安装则升级）
#    bash install.sh --domain bridge.example.com   安装并自动配置 Caddy + HTTPS
#    bash install.sh --port 8321 --yes    非交互安装，无域名，直接 http://IP:8321
#    qqtg-install update                  升级（自动备份数据库，失败自动回滚）
#    qqtg-install uninstall [--purge]     卸载（--purge 同时删除数据）
#    qqtg-install status                  查看状态
#
#  选项:
#    --domain <域名>     配置 Caddy 反向代理并自动申请 HTTPS 证书
#    --port <端口>       面板端口（默认 8321）
#    --bind <地址>       监听地址（有域名默认 127.0.0.1，无域名默认 0.0.0.0）
#    --home <目录>       安装目录（默认 /opt/qqtg-bridge）
#    --ref <分支/标签>   安装指定版本（默认 main）
#    --repo <URL>        代码仓库地址
#    --local             从当前目录安装（开发模式）
#    --no-caddy          即使指定了域名也不安装 Caddy（自行配置反代）
#    --yes / -y          全部使用默认值，不提问
#
#  环境要求: Debian 11+/Ubuntu 20.04+/RHEL 8+/Rocky/Alma/Fedora/Arch/Alpine,
#            x86_64 或 aarch64，root 权限。Python 3.10+（缺少时自动通过 uv 安装）。
# ============================================================================
set -euo pipefail

QQTG_VERSION_SCRIPT="1.0.0"
ORIG_ARGS=("$@")
SELF="${BASH_SOURCE[0]:-$0}"
REPO_DEFAULT="https://github.com/YiranrumengQAQ/QQTG.git"
RAW_BASE_DEFAULT="https://raw.githubusercontent.com/YiranrumengQAQ/QQTG"

# ------------------------------------------------------------------ defaults
ACTION="install"
HOME_DIR="${QQTG_HOME:-/opt/qqtg-bridge}"
SVC_USER="qqtg"
SERVICE="qqtg-bridge"
PORT="${QQTG_PORT:-8321}"
BIND=""
DOMAIN="${QQTG_DOMAIN:-}"
REF="${QQTG_REF:-main}"
REPO="${QQTG_REPO:-$REPO_DEFAULT}"
LOCAL_SRC=""
USE_CADDY="auto"
ASSUME_YES=0
PURGE=0
BACKUP_PATH=""
PY_MIN_MINOR=10

# ------------------------------------------------------------------ ui helpers
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi
ok()    { printf "  %s✓%s %s\n" "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf "  %s!%s %s\n" "$C_YELLOW" "$C_RESET" "$*"; }
fail()  { printf "  %s✗%s %s\n" "$C_RED" "$C_RESET" "$*" >&2; }
info()  { printf "  %s·%s %s\n" "$C_DIM" "$C_RESET" "$*"; }
die()   { echo; fail "$*"; echo; exit 1; }
hr()    { printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_DIM" "$C_RESET"; }
step()  { echo; printf "%s[%s]%s %s%s%s\n" "$C_BLUE" "$1" "$C_RESET" "$C_BOLD" "$2" "$C_RESET"; }
banner() {
  echo; hr
  printf " %sRain Bridge · 雨幕桥接%s  installer v%s\n" "$C_BOLD" "$C_RESET" "$QQTG_VERSION_SCRIPT"
  hr
}

# read from the terminal even when the script itself comes from a pipe
ask() {  # ask "<prompt>" "<default>" -> REPLY
  local prompt="$1" default="${2:-}"
  if [ "$ASSUME_YES" = 1 ] || [ ! -e /dev/tty ]; then REPLY="$default"; return; fi
  if [ -n "$default" ]; then printf "  %s [%s]: " "$prompt" "$default" >/dev/tty; else printf "  %s: " "$prompt" >/dev/tty; fi
  IFS= read -r REPLY </dev/tty || REPLY=""
  REPLY="${REPLY:-$default}"
}
confirm() {  # confirm "<prompt>" [default y|n]
  local prompt="$1" default="${2:-y}" answer
  if [ "$ASSUME_YES" = 1 ] || [ ! -e /dev/tty ]; then [ "$default" = "y" ]; return; fi
  if [ "$default" = "y" ]; then printf "  %s [Y/n]: " "$prompt" >/dev/tty; else printf "  %s [y/N]: " "$prompt" >/dev/tty; fi
  IFS= read -r answer </dev/tty || answer=""
  answer="${answer:-$default}"
  case "$answer" in y|Y|yes|YES) return 0;; *) return 1;; esac
}

# ------------------------------------------------------------------ args
while [ $# -gt 0 ]; do
  case "$1" in
    install|update|upgrade|uninstall|remove|status|backup) ACTION="$1"; [ "$ACTION" = "upgrade" ] && ACTION="update"; [ "$ACTION" = "remove" ] && ACTION="uninstall";;
    --domain) DOMAIN="$2"; shift;;
    --domain=*) DOMAIN="${1#*=}";;
    --port) PORT="$2"; shift;;
    --port=*) PORT="${1#*=}";;
    --bind) BIND="$2"; shift;;
    --bind=*) BIND="${1#*=}";;
    --home) HOME_DIR="$2"; shift;;
    --home=*) HOME_DIR="${1#*=}";;
    --ref) REF="$2"; shift;;
    --ref=*) REF="${1#*=}";;
    --repo) REPO="$2"; shift;;
    --repo=*) REPO="${1#*=}";;
    --local) LOCAL_SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)";;
    --local=*) LOCAL_SRC="${1#*=}";;
    --no-caddy) USE_CADDY="no";;
    --caddy) USE_CADDY="yes";;
    --purge) PURGE=1;;
    -y|--yes) ASSUME_YES=1;;
    -h|--help) sed -n '2,32p' "$SELF" | sed 's/^# \{0,2\}//'; exit 0;;
    *) if [ "$ACTION" = "backup" ] && [ -z "$BACKUP_PATH" ] && [ "${1#-}" = "$1" ]; then BACKUP_PATH="$1"; else die "未知参数: $1（使用 --help 查看帮助）"; fi;;
  esac
  shift
done

APP_DIR="$HOME_DIR/app"
VENV_DIR="$HOME_DIR/venv"
CONFIG_ENV="$HOME_DIR/config/config.env"
UNIT_FILE="/etc/systemd/system/$SERVICE.service"

# ------------------------------------------------------------------ system detection
OS_ID=""; OS_VER=""; OS_NAME=""; PKG=""; ARCH="$(uname -m)"; HAS_SYSTEMD=0
detect_system() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-}"; OS_VER="${VERSION_ID:-}"; OS_NAME="${PRETTY_NAME:-$OS_ID}"
  fi
  if command -v apt-get >/dev/null 2>&1; then PKG="apt"
  elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
  elif command -v yum >/dev/null 2>&1; then PKG="yum"
  elif command -v pacman >/dev/null 2>&1; then PKG="pacman"
  elif command -v apk >/dev/null 2>&1; then PKG="apk"
  elif command -v zypper >/dev/null 2>&1; then PKG="zypper"
  fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then HAS_SYSTEMD=1; fi
}
cpu_cores() { nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1; }
mem_mb()    { awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0; }
disk_free_mb() { df -Pm "$(dirname "$HOME_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0; }
public_ip() {
  local ip
  ip="$(curl -4 -fsS --max-time 4 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 4 https://ifconfig.me 2>/dev/null || true)"
  [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "${ip:-<服务器IP>}"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1 && [ -f "$SELF" ]; then
      warn "需要 root 权限，正在通过 sudo 重新执行…"
      exec sudo -E bash "$SELF" "${ORIG_ARGS[@]}"
    fi
    die "请以 root 身份运行此脚本"
  fi
}

# run a command as the service user (works without sudo)
as_user() {
  if command -v runuser >/dev/null 2>&1; then runuser -u "$SVC_USER" -- "$@"
  elif command -v sudo >/dev/null 2>&1; then sudo -u "$SVC_USER" "$@"
  else su -s /bin/sh "$SVC_USER" -c "$(printf '%q ' "$@")"
  fi
}

create_user() {
  id -u "$SVC_USER" >/dev/null 2>&1 && return 0
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
      || useradd -r -d "$HOME_DIR" -s /sbin/nologin "$SVC_USER" 2>/dev/null || true
  fi
  id -u "$SVC_USER" >/dev/null 2>&1 && return 0
  if command -v adduser >/dev/null 2>&1; then adduser -S -D -H -h "$HOME_DIR" -s /sbin/nologin "$SVC_USER" 2>/dev/null || true; fi
  id -u "$SVC_USER" >/dev/null 2>&1 || die "无法创建系统用户 $SVC_USER"
}

# ------------------------------------------------------------------ packages
pkg_install() {  # best-effort install; never aborts the whole script
  [ $# -eq 0 ] && return 0
  case "$PKG" in
    apt)    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null 2>&1;;
    dnf)    dnf install -y -q "$@" >/dev/null 2>&1;;
    yum)    yum install -y -q "$@" >/dev/null 2>&1;;
    pacman) pacman -Sy --noconfirm --needed "$@" >/dev/null 2>&1;;
    apk)    apk add --no-cache "$@" >/dev/null 2>&1;;
    zypper) zypper --non-interactive install "$@" >/dev/null 2>&1;;
    *)      return 1;;
  esac
}
pkg_update() {
  case "$PKG" in
    apt) apt-get update -qq >/dev/null 2>&1 || warn "apt-get update 出现警告（已忽略）";;
    apk) apk update >/dev/null 2>&1 || true;;
    *) :;;
  esac
}

find_python() {  # prints the best python >= 3.10 or nothing
  local cand v
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      v="$("$cand" -c 'import sys; print(sys.version_info[1] if sys.version_info[0]==3 else 0)' 2>/dev/null || echo 0)"
      if [ "${v:-0}" -ge "$PY_MIN_MINOR" ] && "$cand" -c 'import venv, ensurepip' >/dev/null 2>&1; then
        command -v "$cand"; return 0
      fi
    fi
  done
  return 1
}

install_python() {
  local py
  if py="$(find_python)"; then echo "$py"; return 0; fi
  case "$PKG" in
    apt)    pkg_install python3 python3-venv python3-pip python3-dev || true
            pkg_install python3.12 python3.12-venv || pkg_install python3.11 python3.11-venv || pkg_install python3.10 python3.10-venv || true;;
    dnf|yum) pkg_install python3.12 python3.12-pip || pkg_install python3.11 python3.11-pip || pkg_install python3 python3-pip || true;;
    pacman) pkg_install python python-pip || true;;
    apk)    pkg_install python3 py3-pip || true;;
    zypper) pkg_install python312 python312-pip || pkg_install python311 python311-pip || pkg_install python3 python3-pip || true;;
  esac
  if py="$(find_python)"; then echo "$py"; return 0; fi
  # Universal fallback: uv downloads a standalone CPython build.
  warn "系统软件源没有 Python 3.${PY_MIN_MINOR}+，尝试通过 uv 安装独立 Python…" >&2
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || true
  fi
  if command -v uv >/dev/null 2>&1; then
    uv python install 3.12 >/dev/null 2>&1 || true
    py="$(uv python find 3.12 2>/dev/null || true)"
    if [ -n "$py" ] && [ -x "$py" ]; then echo "$py"; return 0; fi
  fi
  return 1
}

install_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1; then return 0; fi
  case "$PKG" in
    apt|pacman|apk|zypper) pkg_install ffmpeg || true;;
    dnf|yum)
      pkg_install ffmpeg || pkg_install ffmpeg-free || {
        # RHEL family: try EPEL + RPM Fusion (free), non-fatal
        pkg_install epel-release >/dev/null 2>&1 || true
        local rel; rel="$(rpm -E %rhel 2>/dev/null || echo 9)"
        pkg_install "https://download1.rpmfusion.org/free/el/rpmfusion-free-release-${rel}.noarch.rpm" >/dev/null 2>&1 || true
        pkg_install ffmpeg || pkg_install ffmpeg-free || true
      };;
  esac
  if command -v ffmpeg >/dev/null 2>&1; then return 0; fi
  # Static build fallback (x86_64 / aarch64)
  local url="" tmp
  case "$ARCH" in
    x86_64|amd64)  url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz";;
    aarch64|arm64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz";;
  esac
  if [ -n "$url" ]; then
    warn "软件源无 FFmpeg，下载静态构建…"
    tmp="$(mktemp -d)"
    if curl -fsSL --max-time 300 "$url" -o "$tmp/ffmpeg.tar.xz" 2>/dev/null && tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp" 2>/dev/null; then
      install -m 0755 "$tmp"/ffmpeg-*/ffmpeg /usr/local/bin/ffmpeg 2>/dev/null || true
      install -m 0755 "$tmp"/ffmpeg-*/ffprobe /usr/local/bin/ffprobe 2>/dev/null || true
    fi
    rm -rf "$tmp"
  fi
  command -v ffmpeg >/dev/null 2>&1
}

# ------------------------------------------------------------------ firewall
open_firewall() {  # open_firewall <port> [<port>...]
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    for p in "$@"; do ufw allow "$p"/tcp >/dev/null 2>&1 || true; done; ok "ufw 已放行端口 $*"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    for p in "$@"; do firewall-cmd --permanent --add-port="$p"/tcp >/dev/null 2>&1 || true; done
    firewall-cmd --reload >/dev/null 2>&1 || true; ok "firewalld 已放行端口 $*"
  fi
}

# ------------------------------------------------------------------ caddy
install_caddy() {
  if command -v caddy >/dev/null 2>&1; then return 0; fi
  case "$PKG" in
    apt)
      pkg_install debian-keyring debian-archive-keyring apt-transport-https curl gnupg || true
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' 2>/dev/null | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || true
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' 2>/dev/null > /etc/apt/sources.list.d/caddy-stable.list || true
      apt-get update -qq >/dev/null 2>&1 || true
      pkg_install caddy || true;;
    dnf|yum)
      pkg_install 'dnf-command(copr)' >/dev/null 2>&1 || true
      dnf copr enable -y @caddy/caddy >/dev/null 2>&1 || true
      pkg_install caddy || true;;
    pacman|apk|zypper) pkg_install caddy || true;;
  esac
  if command -v caddy >/dev/null 2>&1; then return 0; fi
  # binary fallback
  local arch="" tmp
  case "$ARCH" in x86_64|amd64) arch="amd64";; aarch64|arm64) arch="arm64";; esac
  if [ -n "$arch" ]; then
    tmp="$(mktemp -d)"
    if curl -fsSL --max-time 120 "https://caddyserver.com/api/download?os=linux&arch=${arch}" -o "$tmp/caddy" 2>/dev/null; then
      install -m 0755 "$tmp/caddy" /usr/local/bin/caddy
      id -u caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy 2>/dev/null || true
      mkdir -p /etc/caddy /var/lib/caddy && chown caddy:caddy /var/lib/caddy
      cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy
After=network.target network-online.target
Requires=network-online.target
[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
[Install]
WantedBy=multi-user.target
UNIT
      systemctl daemon-reload
    fi
    rm -rf "$tmp"
  fi
  command -v caddy >/dev/null 2>&1
}

configure_caddy() {
  local caddyfile=/etc/caddy/Caddyfile block
  mkdir -p /etc/caddy
  block="$(cat <<EOF
# --- qqtg-bridge begin ---
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options "nosniff"
        -Server
    }
}
# --- qqtg-bridge end ---
EOF
)"
  touch "$caddyfile"
  if grep -q "qqtg-bridge begin" "$caddyfile"; then
    # replace existing block
    awk -v block="$block" 'BEGIN{skip=0} /# --- qqtg-bridge begin ---/{print block; skip=1; next} /# --- qqtg-bridge end ---/{skip=0; next} skip==0{print}' "$caddyfile" > "$caddyfile.tmp" && mv "$caddyfile.tmp" "$caddyfile"
  else
    # remove the default ":80" site shipped with the package (it would conflict with nothing, but keeps things clean)
    if grep -qE '^\s*:80\s*\{' "$caddyfile" && ! grep -q "reverse_proxy" "$caddyfile"; then : > "$caddyfile"; fi
    printf "\n%s\n" "$block" >> "$caddyfile"
  fi
  caddy fmt --overwrite "$caddyfile" >/dev/null 2>&1 || true
  systemctl enable caddy >/dev/null 2>&1 || true
  if systemctl is-active --quiet caddy; then systemctl reload caddy >/dev/null 2>&1 || systemctl restart caddy; else systemctl restart caddy; fi
}

# ------------------------------------------------------------------ code / venv
fetch_code() {
  mkdir -p "$HOME_DIR"
  if [ -n "$LOCAL_SRC" ]; then
    info "从本地目录安装: $LOCAL_SRC"
    mkdir -p "$APP_DIR"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude 'data' --exclude 'logs' --exclude 'tmp' --exclude '.venv' --exclude 'venv' "$LOCAL_SRC"/ "$APP_DIR"/
    else
      (cd "$LOCAL_SRC" && tar --exclude='.git' --exclude='__pycache__' --exclude='data' --exclude='logs' --exclude='tmp' --exclude='.venv' --exclude='venv' -cf - .) | (cd "$APP_DIR" && tar -xf -)
    fi
    return 0
  fi
  if [ -d "$APP_DIR/.git" ]; then
    info "更新代码 ($REF)…"
    git -C "$APP_DIR" remote set-url origin "$REPO" >/dev/null 2>&1 || true
    git -C "$APP_DIR" fetch --depth 1 origin "$REF" >/dev/null 2>&1 || git -C "$APP_DIR" fetch origin "$REF" >/dev/null 2>&1
    git -C "$APP_DIR" checkout -q -f FETCH_HEAD
  elif [ -d "$APP_DIR" ] && [ -f "$APP_DIR/pyproject.toml" ]; then
    info "已有非 git 安装，重新下载…"
    rm -rf "$APP_DIR.new"
    git clone -q --depth 1 --branch "$REF" "$REPO" "$APP_DIR.new" && rm -rf "$APP_DIR" && mv "$APP_DIR.new" "$APP_DIR"
  else
    info "下载代码 ($REPO @ $REF)…"
    rm -rf "$APP_DIR"
    git clone -q --depth 1 --branch "$REF" "$REPO" "$APP_DIR"
  fi
}

current_commit() { git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo ""; }

make_venv() {
  local py="$1"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "创建虚拟环境…"
    "$py" -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip setuptools wheel >/dev/null 2>&1 || true
  info "安装 Python 依赖…"
  if ! "$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$APP_DIR" >/tmp/qqtg-pip.log 2>&1; then
    tail -20 /tmp/qqtg-pip.log
    die "Python 依赖安装失败（详见 /tmp/qqtg-pip.log）"
  fi
}

install_cli_wrappers() {
  cat > /usr/local/bin/qqtg <<EOF
#!/usr/bin/env bash
# QQTG Bridge CLI wrapper (runs as the service user so file ownership stays correct)
export QQTG_HOME="$HOME_DIR"
if [ "\$(id -u)" -eq 0 ] && id -u "$SVC_USER" >/dev/null 2>&1; then
  if command -v runuser >/dev/null 2>&1; then exec runuser -u "$SVC_USER" -- env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg "\$@"; fi
  if command -v sudo >/dev/null 2>&1; then exec sudo -u "$SVC_USER" env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg "\$@"; fi
fi
exec "$VENV_DIR/bin/python" -m qqtg "\$@"
EOF
  chmod 0755 /usr/local/bin/qqtg
  # keep a copy of this installer for update/uninstall
  if [ -f "$APP_DIR/install.sh" ]; then
    install -m 0755 "$APP_DIR/install.sh" /usr/local/bin/qqtg-install
  else
    [ -f "$SELF" ] && install -m 0755 "$SELF" /usr/local/bin/qqtg-install 2>/dev/null || true
  fi
}

write_unit() {
  local tpl="$APP_DIR/deploy/qqtg-bridge.service"
  if [ -f "$tpl" ]; then
    sed -e "s|__HOME__|$HOME_DIR|g" -e "s|__USER__|$SVC_USER|g" "$tpl" > "$UNIT_FILE"
  else
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Rain Bridge (group message bridge)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$HOME_DIR
Environment=QQTG_HOME=$HOME_DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$CONFIG_ENV
ExecStart=$VENV_DIR/bin/python -m qqtg run
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  fi
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
}

wait_healthy() {
  local i host="127.0.0.1"
  for i in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://$host:$PORT/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

fix_perms() {
  create_user
  mkdir -p "$HOME_DIR"/{config,data,logs,tmp}
  chown -R "$SVC_USER:$SVC_USER" "$HOME_DIR"
  chmod 0750 "$HOME_DIR/config" "$HOME_DIR/data" "$HOME_DIR/logs"
  chmod 0755 "$HOME_DIR" "$HOME_DIR/tmp"   # tmp must be readable by co-located services
  [ -f "$CONFIG_ENV" ] && chmod 0600 "$CONFIG_ENV" || true
}

# read port/bind/public url of an existing installation (command line options win)
PORT_SET_BY_USER=0; for a in "${ORIG_ARGS[@]}"; do case "$a" in --port|--port=*) PORT_SET_BY_USER=1;; esac; done
load_existing_config() {
  [ -f "$CONFIG_ENV" ] || return 0
  local prev_port prev_bind prev_url
  prev_port="$(grep -E '^QQTG_PORT=' "$CONFIG_ENV" 2>/dev/null | cut -d= -f2- || true)"
  prev_bind="$(grep -E '^QQTG_BIND=' "$CONFIG_ENV" 2>/dev/null | cut -d= -f2- || true)"
  prev_url="$(grep -E '^QQTG_PUBLIC_URL=' "$CONFIG_ENV" 2>/dev/null | cut -d= -f2- || true)"
  [ -n "$prev_port" ] && [ "$PORT_SET_BY_USER" = 0 ] && PORT="$prev_port"
  [ -n "$prev_bind" ] && [ -z "$BIND" ] && BIND="$prev_bind"
  [ -z "$DOMAIN" ] && [ -n "$prev_url" ] && case "$prev_url" in https://*) DOMAIN="$(echo "$prev_url" | sed -E 's#^https?://##; s#/.*$##')"; USE_CADDY="${USE_CADDY/auto/skip}";; esac
  return 0
}

# ------------------------------------------------------------------ actions
do_install() {
  banner
  local existing=0
  [ -f "$CONFIG_ENV" ] && existing=1

  step "1/6" "检查系统"
  detect_system
  [ -z "$PKG" ] && die "不支持的系统（未找到 apt/dnf/yum/pacman/apk/zypper）"
  ok "系统: $OS_NAME ($ARCH)"
  local cores mem disk
  cores="$(cpu_cores)"; mem="$(mem_mb)"; disk="$(disk_free_mb)"
  ok "CPU: ${cores} 核   内存: ${mem} MB   可用磁盘: ${disk} MB"
  if [ "$HAS_SYSTEMD" = 1 ]; then ok "systemd 可用"; else warn "未检测到 systemd（容器环境？）——将不会创建系统服务，需手动运行"; fi
  if curl -4 -fsS --max-time 4 https://api.telegram.org >/dev/null 2>&1; then ok "IPv4 可访问 api.telegram.org"; else warn "无法直连 api.telegram.org（如在中国大陆服务器，需自行配置网络/代理或自建 Bot API Server）"; fi
  [ "$mem" -gt 0 ] && [ "$mem" -lt 900 ] && warn "内存不足 1GB：建议在面板「系统」中把媒体处理并发保持为 1"
  [ "$disk" -gt 0 ] && [ "$disk" -lt 1024 ] && warn "磁盘可用空间不足 1GB，媒体转换可能受影响"
  local rec_workers=1; if [ "$cores" -ge 4 ] && [ "$mem" -ge 4096 ]; then rec_workers=2; fi
  info "推荐配置: 媒体处理并发 = $rec_workers（安装后可在面板调整）"

  # interactive questions
  if [ "$existing" = 0 ]; then
    if [ -z "$DOMAIN" ] && [ "$ASSUME_YES" = 0 ]; then
      echo
      info "如果你有解析到本机的域名，可自动配置 Caddy 反向代理 + HTTPS 证书；留空则直接使用 http://IP:端口 访问。"
      ask "面板域名（可留空）" ""; DOMAIN="$REPLY"
    fi
    if [ "$ASSUME_YES" = 0 ]; then ask "面板端口" "$PORT"; PORT="$REPLY"; fi
  else
    info "检测到已安装 ($HOME_DIR)，将执行原地升级并保留数据。"
    load_existing_config
  fi
  case "$PORT" in ''|*[!0-9]*) die "端口必须是数字";; esac
  if [ -z "$BIND" ]; then
    if [ -n "$DOMAIN" ] && [ "$USE_CADDY" != "no" ]; then BIND="127.0.0.1"; else BIND="0.0.0.0"; fi
  fi
  if [ -n "$DOMAIN" ] && [ "$USE_CADDY" = "auto" ]; then USE_CADDY="yes"; fi

  step "2/6" "安装依赖"
  pkg_update
  case "$PKG" in
    apt)    pkg_install ca-certificates curl git tar xz-utils sqlite3 rsync >/dev/null 2>&1 || pkg_install ca-certificates curl git tar xz-utils || true;;
    dnf|yum) pkg_install ca-certificates curl git tar xz sqlite rsync || true;;
    pacman) pkg_install ca-certificates curl git tar xz sqlite rsync || true;;
    apk)    pkg_install ca-certificates curl git tar xz sqlite rsync bash sudo || true;;
    zypper) pkg_install ca-certificates curl git tar xz sqlite3 rsync || true;;
  esac
  command -v git >/dev/null 2>&1 || die "git 安装失败，请手动安装后重试"
  ok "基础工具"
  local PY
  PY="$(install_python)" || die "无法安装 Python 3.${PY_MIN_MINOR}+，请手动安装后重试（Debian/Ubuntu: apt install python3.11 python3.11-venv）"
  ok "Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
  if install_ffmpeg; then ok "FFmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"; else warn "FFmpeg 未安装：语音/贴纸/GIF 转换将不可用（安装后自动生效: 面板 → 一键诊断）"; fi

  step "3/6" "安装 Bridge"
  fetch_code
  ok "代码就绪 ($( [ -n "$LOCAL_SRC" ] && echo local || echo "$REF @ $(current_commit | cut -c1-8)"))"
  make_venv "$PY"
  ok "Python 环境"
  fix_perms
  install_cli_wrappers
  ok "命令行工具: qqtg / qqtg-install"

  step "4/6" "配置服务"
  local public_url="" setup_token=""
  if [ -n "$DOMAIN" ]; then public_url="https://$DOMAIN"; elif [ "$BIND" = "127.0.0.1" ]; then public_url="http://127.0.0.1:$PORT"; else public_url="http://$(public_ip):$PORT"; fi
  local init_args=(init --bind "$BIND" --port "$PORT" --public-url "$public_url" --quiet)
  setup_token="$(as_user env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg "${init_args[@]}" 2>/dev/null | tail -1 || true)"
  fix_perms
  ok "数据库与配置: $CONFIG_ENV"
  if [ "$HAS_SYSTEMD" = 1 ]; then
    write_unit
    ok "systemd 服务: $SERVICE"
  fi

  step "5/6" "启动"
  if [ "$HAS_SYSTEMD" = 1 ]; then
    systemctl restart "$SERVICE"
    if wait_healthy; then ok "服务运行中 (http://$BIND:$PORT)"; else
      fail "服务未能在 30 秒内就绪，最近日志："; journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null || true
      die "启动失败，请根据日志排查（journalctl -u $SERVICE -f）"
    fi
  else
    warn "无 systemd：请手动启动 → runuser -u $SVC_USER -- env QQTG_HOME=$HOME_DIR $VENV_DIR/bin/python -m qqtg run"
  fi

  step "6/6" "网络与反向代理"
  if [ -n "$DOMAIN" ] && [ "$USE_CADDY" = "yes" ] && [ "$HAS_SYSTEMD" = 1 ]; then
    if install_caddy; then
      configure_caddy && ok "Caddy 已配置: https://$DOMAIN（证书自动申请/续期）"
      open_firewall 80 443
    else
      warn "Caddy 安装失败，可参考 $APP_DIR/deploy/nginx.conf.template 手动配置反代"
    fi
  elif [ "$USE_CADDY" = "skip" ]; then
    ok "反向代理配置保持不变 (https://$DOMAIN)"
  elif [ -n "$DOMAIN" ]; then
    info "已跳过 Caddy。请自行把 https://$DOMAIN 反代到 127.0.0.1:$PORT（模板: $APP_DIR/deploy/nginx.conf.template）"
  else
    open_firewall "$PORT"
    warn "当前通过 HTTP 直连访问面板，建议之后配置域名 + HTTPS：qqtg-install --domain 你的域名"
  fi

  echo; hr
  printf " %s安装完成%s\n" "$C_GREEN$C_BOLD" "$C_RESET"
  hr
  printf "  面板地址:   %s%s%s\n" "$C_BOLD" "$public_url" "$C_RESET"
  if [ -n "$setup_token" ]; then
    printf "  初始化令牌: %s%s%s\n" "$C_BOLD" "$setup_token" "$C_RESET"
    printf "  %s首次访问时用此令牌创建 Owner 账号（丢失可执行 qqtg setup-token 重新生成）%s\n" "$C_DIM" "$C_RESET"
  else
    printf "  管理员账号已存在，直接登录即可。\n"
  fi
  echo
  printf "  接下来:\n"
  printf "   1. 打开面板 → 创建管理员\n"
  printf "   2. 连接 Telegram：向 @BotFather 申请 Bot Token（建议关闭 Group Privacy）\n"
  printf "   3. 把机器人拉进群 → 在群里发送 /bridge → 面板中创建桥接（A ↔ B）→ 测试 → 启用\n"
  echo
  printf "  常用命令:  qqtg status | qqtg diagnose | qqtg backup | systemctl status %s\n" "$SERVICE"
  printf "  升级/卸载: qqtg-install update | qqtg-install uninstall\n"
  hr
}

do_update() {
  banner
  detect_system
  [ -d "$APP_DIR" ] || die "未找到安装目录 $APP_DIR，请先安装"
  load_existing_config
  step "1/4" "备份"
  local backup_db prev_commit
  backup_db="$(as_user env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg db-backup 2>/dev/null | tail -1 || true)"
  [ -n "$backup_db" ] && ok "数据库已备份: $backup_db" || warn "数据库备份失败（继续）"
  prev_commit="$(current_commit)"
  step "2/4" "更新代码"
  fetch_code
  ok "代码: $( [ -n "$LOCAL_SRC" ] && echo local || echo "$REF @ $(current_commit | cut -c1-8)")"
  local PY; PY="$(find_python || echo "$VENV_DIR/bin/python")"
  make_venv "$PY"
  fix_perms
  install_cli_wrappers
  step "3/4" "数据库迁移"
  if as_user env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg migrate >/dev/null 2>&1; then ok "迁移完成"; else warn "迁移命令返回错误（服务启动时会再次尝试）"; fi
  step "4/4" "重启服务"
  if [ "$HAS_SYSTEMD" = 1 ]; then
    write_unit
    systemctl restart "$SERVICE"
    if wait_healthy; then
      ok "升级成功，服务运行中"
    else
      fail "新版本启动失败，正在回滚…"
      if [ -n "$prev_commit" ] && [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" checkout -q -f "$prev_commit" || true; fi
      "$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$APP_DIR" >/dev/null 2>&1 || true
      if [ -n "$backup_db" ] && [ -f "$backup_db" ]; then cp -f "$backup_db" "$HOME_DIR/data/bridge.db"; rm -f "$HOME_DIR/data/bridge.db-wal" "$HOME_DIR/data/bridge.db-shm"; chown "$SVC_USER:$SVC_USER" "$HOME_DIR/data/bridge.db"; fi
      systemctl restart "$SERVICE"
      if wait_healthy; then warn "已回滚到 ${prev_commit:0:8}，服务恢复运行"; else fail "回滚后仍无法启动，请查看 journalctl -u $SERVICE"; fi
      exit 1
    fi
  else
    warn "无 systemd，请手动重启服务"
  fi
  hr
}

do_uninstall() {
  banner
  detect_system
  if [ "$ASSUME_YES" = 0 ] && [ "$PURGE" = 0 ] && ! confirm "确定卸载 QQTG Bridge？（数据目录 $HOME_DIR 默认保留）" n; then echo "已取消"; exit 0; fi
  if [ "$HAS_SYSTEMD" = 1 ]; then
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"; systemctl daemon-reload
    ok "服务已移除"
    if [ -f /etc/caddy/Caddyfile ] && grep -q "qqtg-bridge begin" /etc/caddy/Caddyfile; then
      awk '/# --- qqtg-bridge begin ---/{skip=1; next} /# --- qqtg-bridge end ---/{skip=0; next} skip==0{print}' /etc/caddy/Caddyfile > /etc/caddy/Caddyfile.tmp && mv /etc/caddy/Caddyfile.tmp /etc/caddy/Caddyfile
      systemctl reload caddy >/dev/null 2>&1 || true
      ok "已移除 Caddy 站点配置（Caddy 本身保留）"
    fi
  fi
  rm -f /usr/local/bin/qqtg /usr/local/bin/qqtg-install
  ok "命令行工具已移除"
  if [ "$PURGE" = 1 ] || confirm "同时删除安装目录与全部数据 ($HOME_DIR)？" n; then
    rm -rf "$HOME_DIR"
    userdel "$SVC_USER" >/dev/null 2>&1 || true
    ok "数据已删除"
  else
    rm -rf "$APP_DIR" "$VENV_DIR"
    ok "程序已删除，数据保留在 $HOME_DIR（config/data/logs）"
  fi
  hr
}

do_status() {
  detect_system
  if [ "$HAS_SYSTEMD" = 1 ]; then systemctl --no-pager --lines=5 status "$SERVICE" 2>/dev/null || true; echo; fi
  if [ -x /usr/local/bin/qqtg ]; then /usr/local/bin/qqtg status; else warn "未安装"; fi
}

do_backup() {
  [ -x /usr/local/bin/qqtg ] || die "未安装"
  local out="${1:-/root/qqtg-backup-$(date +%Y%m%d-%H%M%S).json}"
  as_user env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg backup "$HOME_DIR/data/last-backup.json" >/dev/null
  cp -f "$HOME_DIR/data/last-backup.json" "$out"; chmod 0600 "$out"
  ok "配置备份: $out"
  info "数据库快照: $(as_user env QQTG_HOME="$HOME_DIR" "$VENV_DIR/bin/python" -m qqtg db-backup | tail -1)"
}

# ------------------------------------------------------------------ main
case "$ACTION" in
  install)   require_root; do_install;;
  update)    require_root; do_update;;
  uninstall) require_root; do_uninstall;;
  status)    do_status;;
  backup)    require_root; do_backup "$BACKUP_PATH";;
esac
