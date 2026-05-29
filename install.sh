#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="Mihomo Toolbox"
GH_PROXY="${GH_PROXY:-https://gh-proxy.com/}"
REPO_URL="${MIHOMO_REPO_URL:-https://github.com/anxiaoyang666/mihomo.git}"
BRANCH="${MIHOMO_BRANCH:-main}"
WEB_PORT="${WEB_PORT:-7838}"
INSTALL_DIR="/etc/mihomo"
MANAGER_DIR="$INSTALL_DIR/manager"
TMP_DIR=""

red() { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die() { red "ERROR: $*"; exit 1; }

cleanup() {
  if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "请使用 root 执行安装命令。"
  fi
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_packages() {
  if has_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl wget unzip gzip git python3 python3-flask python3-yaml iproute2 iptables nano cron
  elif has_cmd dnf; then
    dnf install -y ca-certificates curl wget unzip gzip git python3 python3-flask python3-pyyaml iproute iptables nano cronie
  elif has_cmd yum; then
    yum install -y ca-certificates curl wget unzip gzip git python3 python3-flask PyYAML iproute iptables nano cronie
  else
    die "未找到 apt-get/dnf/yum，无法自动安装依赖。"
  fi
}

url_with_proxy() {
  local url="$1"
  if [ -n "$GH_PROXY" ] && [[ "$url" == https://github.com/* || "$url" == https://raw.githubusercontent.com/* ]]; then
    printf '%s%s' "$GH_PROXY" "$url"
  else
    printf '%s' "$url"
  fi
}

git_clone_project() {
  local target="$1"
  local proxy_url
  proxy_url="$(url_with_proxy "$REPO_URL")"

  if git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$target"; then
    return
  fi
  yellow "直接拉取失败，尝试通过 GitHub 代理拉取..."
  rm -rf "$target"
  git clone --depth 1 --branch "$BRANCH" "$proxy_url" "$target"
}

source_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -d "$script_dir/remote-root" ]; then
    printf '%s' "$script_dir"
    return
  fi

  TMP_DIR="$(mktemp -d)"
  git_clone_project "$TMP_DIR/mihomo"
  [ -d "$TMP_DIR/mihomo/remote-root" ] || die "仓库中没有 remote-root 目录。"
  printf '%s' "$TMP_DIR/mihomo"
}

rand_secret() {
  if has_cmd openssl; then
    openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-'
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24), end="")
PY
  fi
}

copy_payload() {
  local root="$1"
  local payload="$root/remote-root"
  [ -d "$payload" ] || die "安装包缺少 remote-root。"

  mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/scripts" "$INSTALL_DIR/templates" "$MANAGER_DIR" "$INSTALL_DIR/backup" /etc/systemd/system /usr/bin

  install -m 0755 "$payload/usr/bin/mihomo" /usr/bin/mihomo
  cp -a "$payload/etc/mihomo/manager/." "$MANAGER_DIR/"
  rm -rf "$MANAGER_DIR/__pycache__"
  find "$MANAGER_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +

  cp -a "$payload/etc/mihomo/scripts/." "$INSTALL_DIR/scripts/"
  cp -a "$payload/etc/mihomo/templates/." "$INSTALL_DIR/templates/"

  if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    if [ -f "$payload/etc/mihomo/config.example.yaml" ]; then
      cp "$payload/etc/mihomo/config.example.yaml" "$INSTALL_DIR/config.yaml"
    fi
  fi
  [ -f "$payload/etc/mihomo/config.example.yaml" ] && cp "$payload/etc/mihomo/config.example.yaml" "$INSTALL_DIR/config.example.yaml"

  install -m 0644 "$payload/etc/systemd/system/mihomo.service" /etc/systemd/system/mihomo.service
  install -m 0644 "$payload/etc/systemd/system/mihomo-manager.service" /etc/systemd/system/mihomo-manager.service
  install -m 0644 "$payload/etc/systemd/system/force-ip-forward.service" /etc/systemd/system/force-ip-forward.service
}

write_env_file() {
  local user pass session_secret
  user="${WEB_USER:-admin}"
  pass="${WEB_SECRET:-$(rand_secret)}"
  session_secret="$(rand_secret)$(rand_secret)"

  if [ -f "$INSTALL_DIR/.env" ] && [ "${MIHOMO_KEEP_ENV:-1}" = "1" ]; then
    yellow "保留已有 $INSTALL_DIR/.env"
    return
  fi

  cat > "$INSTALL_DIR/.env" <<EOF
WEB_SESSION_SECRET="$session_secret"
WEB_USER="$user"
WEB_SECRET="$pass"
WEB_PORT="$WEB_PORT"
MIHOMO_PANEL_REPO_URL="$REPO_URL"
MIHOMO_PANEL_BRANCH="$BRANCH"
MIHOMO_PATH="$INSTALL_DIR"
SCRIPT_PATH="$INSTALL_DIR/scripts"
GH_PROXY="$GH_PROXY"
CONFIG_MODE="raw"
SUB_URL_RAW=""
SUB_URL_AIRPORT=""
LOCAL_CIDR=""
BACKUP_KEEP_COUNT="20"
EOF
  chmod 600 "$INSTALL_DIR/.env"
}

install_core_if_missing() {
  if [ -x /usr/bin/mihomo-core ]; then
    return
  fi
  yellow "未检测到 mihomo-core，正在安装最新内核..."
  bash "$INSTALL_DIR/scripts/install_kernel.sh" auto
  if [ -f "$INSTALL_DIR/mihomo" ]; then
    install -m 0755 "$INSTALL_DIR/mihomo" /usr/bin/mihomo-core
  fi
}

enable_services() {
  systemctl daemon-reload
  systemctl enable force-ip-forward >/dev/null 2>&1 || true
  systemctl enable mihomo-manager >/dev/null 2>&1 || true
  systemctl restart force-ip-forward >/dev/null 2>&1 || true
  systemctl restart mihomo-manager
  if [ -x /usr/bin/mihomo-core ] && [ -f "$INSTALL_DIR/config.yaml" ]; then
    systemctl enable mihomo >/dev/null 2>&1 || true
    systemctl restart mihomo || yellow "mihomo 内核服务启动失败，请在 Web 面板检查配置文件。"
  fi
}

print_summary() {
  local ip user pass
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="服务器IP"
  user="$(grep '^WEB_USER=' "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"')"
  pass="$(grep '^WEB_SECRET=' "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d '"')"
  green "$PROJECT_NAME 安装完成"
  printf '\n'
  printf 'Web 面板: http://%s:%s/\n' "$ip" "$WEB_PORT"
  printf '用户名: %s\n' "$user"
  printf '密码: %s\n' "$pass"
  printf '\n'
  printf '命令行管理: mihomo\n'
}

main() {
  require_root
  yellow "安装依赖..."
  install_packages
  local root
  root="$(source_root)"
  yellow "安装 Mihomo 面板和脚本..."
  copy_payload "$root"
  write_env_file
  install_core_if_missing
  yellow "启动服务..."
  enable_services
  print_summary
}

main "$@"
