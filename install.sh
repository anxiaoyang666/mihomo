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

# 状态信息走 stderr，避免被 $(...) 捕获进变量
red() { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[0;32m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*" >&2; }
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

# 结果写入 SOURCE_ROOT 而不是 echo，这样 TMP_DIR 能留在当前 shell 供 cleanup 使用
SOURCE_ROOT=""
source_root() {
  local script_dir=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "$script_dir" ] && [ -d "$script_dir/remote-root" ]; then
    SOURCE_ROOT="$script_dir"
    return
  fi

  TMP_DIR="$(mktemp -d)"
  git_clone_project "$TMP_DIR/mihomo"
  [ -d "$TMP_DIR/mihomo/remote-root" ] || die "仓库中没有 remote-root 目录。"
  SOURCE_ROOT="$TMP_DIR/mihomo"
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

# .env 的每一行都用 Python shlex.quote 引用：bash 的 printf %q 遇到换行/控制字符会输出 $'...'，
# 面板 app.py 和 scripts/envutil.sh 用 shlex 解析时认不全这种形式。
write_env_line() {
  local key="$1"
  local value="$2"
  python3 -c 'import shlex, sys; print(sys.argv[1] + "=" + shlex.quote(sys.argv[2]))' "$key" "$value"
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
  # 日志轮转：mihomo.log 由 shell 重定向写入，不轮转会一直涨
  if [ -f "$payload/etc/logrotate.d/mihomo" ]; then
    mkdir -p /etc/logrotate.d
    install -m 0644 "$payload/etc/logrotate.d/mihomo" /etc/logrotate.d/mihomo
  fi
}

# env_get / env_upsert 和 remote-root/etc/mihomo/scripts/envutil.sh 的 get_env / upsert_env 是同一套逻辑。
# 安装脚本在 scripts/ 目录装好之前就要读写 .env（而且可能是 curl | bash 单文件运行），所以这里保留一份副本；
# 改动引用/解析规则时两边要一起改。

# 读 .env 里某个键的值（.env 是 shell 语法，用 shlex 解析；未加引号的值里的 # 不是注释）
env_get() {
  local key="$1"
  [ -f "$INSTALL_DIR/.env" ] || return 0
  python3 - "$INSTALL_DIR/.env" "$key" <<'PY'
import shlex, sys
path, key = sys.argv[1], sys.argv[2]
value = ""
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, _, raw = line.partition("=")
    if name.strip() != key:
        continue
    try:
        parts = shlex.split(raw.strip(), comments=False, posix=True)
        value = parts[0] if parts else ""
    except ValueError:
        value = raw.strip()
print(value, end="")
PY
}

# 在 .env 里新增或替换一个键（shlex 引用，0600 临时文件 + 原子替换）
env_upsert() {
  local key="$1"
  local value="$2"
  python3 - "$INSTALL_DIR/.env" "$key" "$value" <<'PY'
import os, pathlib, re, shlex, sys
path = pathlib.Path(sys.argv[1])
key, value = sys.argv[2], sys.argv[3]
line = f"{key}={shlex.quote(value)}\n"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
pattern = re.compile(rf"^\s*{re.escape(key)}=")
for index, existing in enumerate(lines):
    if pattern.match(existing):
        lines[index] = line
        break
else:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(line)
tmp = path.with_name(f".env.{os.getpid()}.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("".join(lines))
os.replace(tmp, path)
PY
  chmod 600 "$INSTALL_DIR/.env"
}

write_env_file() {
  local user pass session_secret sync_token api_secret
  user="${WEB_USER:-admin}"
  pass="${WEB_SECRET:-$(rand_secret)}"
  session_secret="$(rand_secret)$(rand_secret)"
  sync_token="$(rand_secret)"
  api_secret="$(rand_secret)"

  if [ -f "$INSTALL_DIR/.env" ] && [ "${MIHOMO_KEEP_ENV:-1}" = "1" ]; then
    yellow "保留已有 $INSTALL_DIR/.env"
    return
  fi

  {
    write_env_line "WEB_SESSION_SECRET" "$session_secret"
    write_env_line "WEB_USER" "$user"
    write_env_line "WEB_SECRET" "$pass"
    write_env_line "WEB_PORT" "$WEB_PORT"
    write_env_line "MIHOMO_PANEL_REPO_URL" "$REPO_URL"
    write_env_line "MIHOMO_PANEL_BRANCH" "$BRANCH"
    write_env_line "MIHOMO_PATH" "$INSTALL_DIR"
    write_env_line "SCRIPT_PATH" "$INSTALL_DIR/scripts"
    write_env_line "GH_PROXY" "$GH_PROXY"
    write_env_line "MIHOMO_API_SECRET" "$api_secret"
    write_env_line "CONFIG_MODE" "raw"
    write_env_line "SUB_URL_RAW" ""
    write_env_line "SUB_URL_AIRPORT" ""
    write_env_line "LOCAL_CIDR" ""
    write_env_line "BACKUP_KEEP_COUNT" "20"
    write_env_line "CRON_SUB_ENABLED" "true"
    write_env_line "CRON_SUB_SCHED" "0 5 * * *"
    write_env_line "CRON_SUB_MODE" "daily"
    write_env_line "CRON_SUB_TIME" "05:00"
    write_env_line "RULE_SYNC_TOKEN" "$sync_token"
    write_env_line "RULE_SYNC_ENABLED" "false"
    write_env_line "RULE_SYNC_PEERS" ""
  } > "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
}

# 控制器 (external-controller, 9090) 必须对局域网开放：Zashboard/MetaCubeXD 这类外部 Dashboard 是在
# 用户浏览器里直接连 9090 的（面板的"打开 Dashboard"按钮和 CLI 的面板信息都指向 http://<ip>:9090/ui），
# 所以不能把它绑回 127.0.0.1，而是给它配一个随机 secret：
#   - 写进 config.yaml 的 secret:（内核用它鉴权）
#   - 写进 .env 的 MIHOMO_API_SECRET（面板 app.py 用它访问控制器，update_subscription.sh 用它回填新配置）
# 以 config.yaml 里已有的非空 secret 为准（升级/重装时不改用户已经填好的密钥）。
sync_controller_secret() {
  local api_secret config_secret
  api_secret="$(env_get MIHOMO_API_SECRET)"
  config_secret=""
  if [ -f "$INSTALL_DIR/config.yaml" ]; then
    config_secret="$(sed -n 's/^secret:[[:space:]]*//p' "$INSTALL_DIR/config.yaml" | head -n 1 | tr -d '"'"'"' ' | tr -d '\r')"
  fi
  if [ -n "$config_secret" ]; then
    api_secret="$config_secret"
  elif [ -z "$api_secret" ]; then
    api_secret="$(rand_secret)"
  fi
  if [ "$(env_get MIHOMO_API_SECRET)" != "$api_secret" ]; then
    env_upsert "MIHOMO_API_SECRET" "$api_secret"
  fi
  if [ -f "$INSTALL_DIR/config.yaml" ] && [ -z "$config_secret" ]; then
    python3 - "$INSTALL_DIR/config.yaml" "$api_secret" <<'PY'
import re, sys
path, secret = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
line = 'secret: "%s"' % secret.replace('\\', '\\\\').replace('"', '\\"')
# 替换串用 lambda 传入，避免密钥里的反斜杠被 re.sub 当转义解释
if re.search(r"(?m)^secret\s*:", text):
    text = re.sub(r"(?m)^secret\s*:.*$", lambda m: line, text, count=1)
elif re.search(r"(?m)^external-controller\s*:", text):
    text = re.sub(r"(?m)^(external-controller\s*:.*)$", lambda m: m.group(1) + "\n" + line, text, count=1)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
open(path, "w", encoding="utf-8").write(text)
PY
    green "已为 external-controller 生成访问密钥 (MIHOMO_API_SECRET)"
  fi
}

# 订阅定时任务：以前只有在面板"设置"页点过保存才会写 crontab，装完就不管的话永远不会自动更新。
# 这里装完直接按 .env 的 CRON_SUB_* 写好；旧 .env 没有这两个键时补上默认值（每天 05:00）。
# 显式写了 CRON_SUB_ENABLED=false 的保持关闭。输出追加到日志，面板和 .last_subscription 都能看到结果。
install_cron_jobs() {
  local enabled sched tmp
  enabled="$(env_get CRON_SUB_ENABLED)"
  sched="$(env_get CRON_SUB_SCHED)"
  if [ -z "$enabled" ]; then
    env_upsert "CRON_SUB_ENABLED" "true"
    enabled="true"
  fi
  if [ -z "$sched" ]; then
    sched="0 5 * * *"
    env_upsert "CRON_SUB_SCHED" "$sched"
    env_upsert "CRON_SUB_MODE" "daily"
    env_upsert "CRON_SUB_TIME" "05:00"
  fi
  systemctl enable --now cron >/dev/null 2>&1 || systemctl enable --now crond >/dev/null 2>&1 || true
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -F -v -- '# JOB_SUB' > "$tmp" || true
  if [ "$enabled" = "true" ]; then
    printf '%s bash %s/scripts/update_subscription.sh >> /var/log/mihomo-subscription.log 2>&1 # JOB_SUB\n' "$sched" "$INSTALL_DIR" >> "$tmp"
  fi
  if [ -s "$tmp" ]; then crontab "$tmp"; else crontab -r 2>/dev/null || true; fi
  rm -f "$tmp"
  [ "$enabled" = "true" ] && green "已设置订阅定时任务：$sched（日志 /var/log/mihomo-subscription.log）"
  return 0
}

# 内核装不上（网络不通、GitHub 被墙）不应该让整个安装失败：面板装好以后可以在 Web 里/CLI 里再装内核
install_core_if_missing() {
  if [ -x /usr/bin/mihomo-core ]; then
    return
  fi
  yellow "未检测到 mihomo-core，正在安装最新内核..."
  if ! bash "$INSTALL_DIR/scripts/install_kernel.sh" auto; then
    yellow "警告：内核安装失败，面板和脚本已安装。稍后可执行 mihomo -> [1] 更新/修复内核 重试。"
    return 0
  fi
  [ -x /usr/bin/mihomo-core ] || yellow "警告：内核安装脚本结束但 /usr/bin/mihomo-core 不存在，请稍后重试。"
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
  local ip user pass api_secret
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="服务器IP"
  user="$(env_get WEB_USER)"
  pass="$(env_get WEB_SECRET)"
  api_secret="$(env_get MIHOMO_API_SECRET)"
  green "$PROJECT_NAME 安装完成"
  printf '\n'
  printf 'Web 面板: http://%s:%s/\n' "$ip" "$WEB_PORT"
  printf '用户名: %s\n' "$user"
  printf '密码: %s\n' "$pass"
  printf '\n'
  printf '内核 Dashboard: http://%s:9090/ui\n' "$ip"
  printf 'Dashboard 密钥 (secret): %s\n' "$api_secret"
  printf '\n'
  printf '命令行管理: mihomo\n'
  if [ ! -x /usr/bin/mihomo-core ]; then
    yellow "注意：内核尚未安装成功，请执行 mihomo -> [1] 更新/修复内核。"
  fi
}

main() {
  require_root
  yellow "安装依赖..."
  install_packages
  source_root
  local root="$SOURCE_ROOT"
  yellow "安装 Mihomo 面板和脚本..."
  copy_payload "$root"
  write_env_file
  sync_controller_secret
  install_cron_jobs
  install_core_if_missing
  yellow "启动服务..."
  enable_services
  print_summary
}

main "$@"
