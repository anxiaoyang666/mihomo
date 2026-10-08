#!/bin/bash
# update_subscription.sh - 订阅更新 (支持 Raw/Airport + 自动防回环注入 + 优雅通知)

MIHOMO_DIR="/etc/mihomo"
ENV_FILE="${MIHOMO_DIR}/.env"
CONFIG_FILE="${MIHOMO_DIR}/config.yaml"
TEMPLATE_FILE="${MIHOMO_DIR}/templates/default.yaml"
BACKUP_DIR="${MIHOMO_DIR}/backup"
NOTIFY_SCRIPT="${MIHOMO_DIR}/scripts/notify.sh"
TMP_DIR="$(mktemp -d)"
TEMP_NEW="${TMP_DIR}/config_generated.yaml"
# 每次运行的结果（时间 / 状态 / 一句话说明），面板概览页读取它显示"上次订阅更新"
STATE_FILE="${MIHOMO_DIR}/.last_subscription"
RESULT_MSG=""

on_exit() {
    local code=$?
    rm -rf "$TMP_DIR"
    local status="ok"
    [ "$code" -eq 0 ] || status="failed"
    printf '%s\t%s\t%s\n' "$(date +%s)" "$status" "${RESULT_MSG:-退出码 $code}" > "$STATE_FILE" 2>/dev/null || true
}
trap on_exit EXIT
echo "===== $(date '+%F %T') 开始订阅更新 ====="

# 1. 加载环境变量
if [ -f "$ENV_FILE" ]; then source "$ENV_FILE"; fi
BACKUP_KEEP_COUNT="${BACKUP_KEEP_COUNT:-10}"
# 很多机场按 User-Agent 决定返回什么格式：不是 Clash 系的 UA 会拿到 base64 节点列表而不是 YAML 配置
SUB_USER_AGENT="${SUB_USER_AGENT:-clash.meta}"

# TLS 默认严格校验；只有 .env 里显式写 ALLOW_INSECURE_TLS=true 才跳过证书检查
WGET_OPTS=(--timeout=30 --tries=2 --user-agent="$SUB_USER_AGENT")
if [ "$ALLOW_INSECURE_TLS" == "true" ]; then
    WGET_OPTS+=(--no-check-certificate)
fi

mkdir -p "$BACKUP_DIR"
mkdir -p "${MIHOMO_DIR}/providers"

# 只保留最新的 BACKUP_KEEP_COUNT 份备份（和面板 app.py 的 prune_config_backups 同一套文件名模式）
prune_backups() {
    local keep="$BACKUP_KEEP_COUNT"
    [[ "$keep" =~ ^[0-9]+$ ]] && [ "$keep" -ge 1 ] || keep=10
    ls -1t "$BACKUP_DIR"/config_*.yaml "$BACKUP_DIR"/config.before-rule-sync.*.yaml 2>/dev/null \
        | tail -n +$((keep + 1)) \
        | while read -r old; do rm -f "$old"; done
}

# ==========================================
# 第一阶段：生成基础配置 (Raw 或 Airport)
# ==========================================

if [ "$CONFIG_MODE" == "raw" ]; then
    # --- Raw 模式 (配置托管) ---
    if [ -z "$SUB_URL_RAW" ]; then
        echo "❌ [配置托管] 未配置订阅链接，跳过。"
        RESULT_MSG="未配置托管订阅链接，跳过"
        exit 0
    fi
    echo "⬇️  [配置托管] 正在下载完整配置..."
    wget "${WGET_OPTS[@]}" -O "$TEMP_NEW" "$SUB_URL_RAW" >/dev/null 2>&1

    if [ $? -ne 0 ] || [ ! -s "$TEMP_NEW" ]; then
        echo "❌ 下载失败。"
        RESULT_MSG="托管配置下载失败"
        bash "$NOTIFY_SCRIPT" "❌ 更新失败" "无法下载托管配置。"
        rm -f "$TEMP_NEW"
        exit 1
    fi
else
    # --- Airport 模式 (节点订阅) ---
    if [ ! -f "$TEMPLATE_FILE" ]; then
        echo "❌ 模板文件缺失: $TEMPLATE_FILE"
        exit 1
    fi
    if [ -z "$SUB_URL_AIRPORT" ]; then
        echo "❌ [节点订阅] 未配置机场链接。"
        RESULT_MSG="未配置机场订阅链接，跳过"
        exit 0
    fi
    echo "🔨 [节点订阅] 正在构建配置文件..."
    export SUB_URL_AIRPORT

    # 路径走 argv，不拼进 Python 源码
    python3 - "$TEMPLATE_FILE" "$TEMP_NEW" <<'PY'
import sys, yaml, os
template_path = sys.argv[1]
output_path = sys.argv[2]
urls_raw = os.environ.get('SUB_URL_AIRPORT', '').replace('|', '\n').replace('\\n', '\n')

def load_yaml(path):
    if not os.path.exists(path): return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

try:
    config = load_yaml(template_path)
    url_list = [line.strip() for line in urls_raw.split('\n') if line.strip()]
    if not url_list:
        print('Error: No valid URLs found')
        sys.exit(1)

    providers = {}
    for index, url in enumerate(url_list):
        name = f'Airport_{index+1:02d}'
        providers[name] = {
            'type': 'http',
            'url': url,
            'interval': 86400,
            'path': f'./providers/airport_{index+1:02d}.yaml',
            'health-check': {'enable': True, 'interval': 600, 'url': 'https://www.gstatic.com/generate_204'}
        }
    config['proxy-providers'] = providers
    
    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
except Exception as e:
    print(f'Error: {e}')
    sys.exit(1)
PY
    if [ $? -ne 0 ]; then
        echo "❌ 生成配置失败。"
        RESULT_MSG="按模板生成配置失败"
        bash "$NOTIFY_SCRIPT" "❌ 生成失败" "YAML 处理错误。"
        rm -f "$TEMP_NEW"
        exit 1
    fi
fi

# ==========================================
# 第二阶段：通用补丁 (注入防回环规则)
# ==========================================

# 只有当 LOCAL_CIDR 不为空时才执行注入
if [ -n "$LOCAL_CIDR" ]; then
    echo "🛡️ 检测到防回环设置 ($LOCAL_CIDR)，正在注入规则..."
    export LOCAL_CIDR

    python3 - "$TEMP_NEW" <<'PY'
import sys, yaml, os

config_path = sys.argv[1]
local_cidr = os.environ.get('LOCAL_CIDR', '').strip()

try:
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    # 构造防回环规则
    # 格式: IP-CIDR,192.168.1.0/24,DIRECT,no-resolve
    loop_rule = f'IP-CIDR,{local_cidr},DIRECT,no-resolve'

    # 同步 TUN 路由排除，避免本地网段被 auto-route 接管
    if 'tun' not in config or not isinstance(config['tun'], dict):
        config['tun'] = {}
    route_exclude = config['tun'].get('route-exclude-address')
    if not isinstance(route_exclude, list):
        route_exclude = []
    route_exclude = [cidr for cidr in route_exclude if cidr != local_cidr]
    route_exclude.insert(0, local_cidr)
    config['tun']['route-exclude-address'] = route_exclude
    print(f'✅ 已同步 TUN 路由排除: {local_cidr}')

    # 确保 rules 列表存在
    if 'rules' not in config or config['rules'] is None:
        config['rules'] = []

    # 【关键】将规则插入到第一位 (Index 0)
    # 避免重复插入
    config['rules'] = [r for r in config['rules'] if r != loop_rule]
    config['rules'].insert(0, loop_rule)
    print(f'✅ 已插入规则: {loop_rule}')

    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)

except Exception as e:
    print(f'⚠️ 防回环注入失败: {e}')
    # 注意：这里我们不退出 exit 1，因为即使注入失败，主体配置可能还是能用的，
    # 但建议在日志里看到警告。
PY
fi

# ==========================================
# 第二阶段 B：保留控制器密钥
# ==========================================

# 新配置从模板/订阅重新生成，external-controller 的 secret 会变回空。
# 面板用 .env 的 MIHOMO_API_SECRET 访问控制器（app.py mihomo_controller_settings），所以以它为准；
# .env 没有时退回旧 config.yaml 里的 secret。两边都没有就不动（保持无密钥状态，由安装脚本负责生成）。
export MIHOMO_API_SECRET
python3 - "$CONFIG_FILE" "$TEMP_NEW" <<'PY'
import os, re, sys, yaml

old_path, new_path = sys.argv[1], sys.argv[2]

def read_secret_from_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            match = re.match(r'^\s*secret\s*:\s*(.*?)\s*$', line)
            if match:
                return match.group(1).strip().strip('"').strip("'")
    return ""

secret = os.environ.get('MIHOMO_API_SECRET', '').strip() or read_secret_from_file(old_path)
if not secret:
    sys.exit(0)
try:
    with open(new_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        sys.exit(0)
    if config.get('secret') == secret:
        sys.exit(0)
    config['secret'] = secret
    with open(new_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
    print('✅ 已保留控制器密钥 (secret)。')
except Exception as e:
    print(f'⚠️ 控制器密钥保留失败: {e}')
PY

# ==========================================
# 第三阶段：保留面板里的同步规则
# ==========================================

# 新配置是从模板/订阅重新生成的，面板保存的强制直连/强制代理规则块只存在于旧 config.yaml 里，
# 不带过来就会在每次订阅更新时丢失。
if [ -f "$CONFIG_FILE" ] && [ -f "${MIHOMO_DIR}/manager/app.py" ]; then
    python3 - "$CONFIG_FILE" "$TEMP_NEW" <<'PY'
import sys
sys.path.insert(0, "/etc/mihomo/manager")
try:
    import app
    if app.reapply_sync_blocks(sys.argv[1], sys.argv[2]):
        print("✅ 已保留面板同步规则。")
except Exception as e:
    print(f"⚠️ 同步规则保留失败: {e}")
PY
fi

# ==========================================
# 第四阶段：校验、应用与通知
# ==========================================

if [ ! -s "$TEMP_NEW" ]; then
    rm -f "$TEMP_NEW"
    RESULT_MSG="生成的新配置为空"
    bash "$NOTIFY_SCRIPT" "❌ 订阅更新失败" "生成的新配置为空，已保留当前配置。"
    exit 1
fi

# 应用前先用内核校验，坏配置（比如订阅过期返回的 HTML 页面）不能覆盖正在用的配置
CORE_BIN="/usr/bin/mihomo-core"
if [ -x "$CORE_BIN" ]; then
    if ! CHECK_OUT="$("$CORE_BIN" -t -d "$MIHOMO_DIR" -f "$TEMP_NEW" 2>&1)"; then
        echo "❌ 新配置校验失败，已保留当前配置。"
        echo "$CHECK_OUT" | tail -n 5
        RESULT_MSG="新配置校验失败，已保留当前配置"
        bash "$NOTIFY_SCRIPT" "❌ 订阅更新失败" "新配置校验失败，已保留当前配置。"
        exit 1
    fi
fi

FILE_CHANGED=0
if [ -f "$CONFIG_FILE" ]; then
    if cmp -s "$TEMP_NEW" "$CONFIG_FILE"; then
        echo "✅ 配置无变更。"
        FILE_CHANGED=0
    else
        echo "⚠️  配置有变更。"
        FILE_CHANGED=1
    fi
else
    FILE_CHANGED=1
fi

if [ "$FILE_CHANGED" -eq 1 ]; then
    BACKUP_FILE="${BACKUP_DIR}/config_$(date +%Y%m%d%H%M%S).yaml"
    [ -f "$CONFIG_FILE" ] && cp "$CONFIG_FILE" "$BACKUP_FILE"
    prune_backups
    mv "$TEMP_NEW" "$CONFIG_FILE"
    systemctl restart mihomo
    sleep 3
    # 重启后没起来就回滚，不然 Restart=always 会让网关一直崩溃循环
    if ! systemctl is-active --quiet mihomo; then
        echo "❌ mihomo 重启失败，正在回滚到更新前的配置..."
        if [ -f "$BACKUP_FILE" ]; then
            cp "$BACKUP_FILE" "$CONFIG_FILE"
            systemctl restart mihomo
        fi
        RESULT_MSG="新配置启动失败，已回滚"
        bash "$NOTIFY_SCRIPT" "❌ 订阅更新失败" "新配置启动失败，已回滚到更新前的配置。"
        exit 1
    fi
    echo "🎉 更新完成并重启。"
    RESULT_MSG="配置已更新并重启"
    
    # --- 文案转换逻辑 ---
    if [ "$CONFIG_MODE" == "raw" ]; then
        MODE_NAME="配置托管"
    else
        MODE_NAME="节点订阅"
    fi
    
    bash "$NOTIFY_SCRIPT" "♻️ 订阅更新成功" "模式: ${MODE_NAME}"
else
    rm -f "$TEMP_NEW"
    RESULT_MSG="配置无变更"
fi

# ==========================================
# 第五阶段：机场模式下让内核立刻重新拉取节点
# ==========================================

# 机场模式的 config.yaml 只写了订阅 URL，节点列表由内核按 proxy-providers 的 interval 自己拉取，
# 所以上面经常是"配置无变更"。要让"每天更新订阅"真的更新节点，得通过控制器 API 让内核立刻刷新：
#   PUT /providers/proxies/<name>
# 重启也不会触发刷新（内核会直接用缓存的 providers/*.yaml），所以无论配置有没有变都执行。
refresh_proxy_providers() {
    [ -f "$CONFIG_FILE" ] || return 0
    systemctl is-active --quiet mihomo || { echo "ℹ️  内核未运行，跳过节点刷新。"; return 0; }

    local controller secret
    controller="$(sed -n 's/^external-controller:[[:space:]]*//p' "$CONFIG_FILE" | head -n 1 | tr -d '"'"'"' \r' | sed 's/[[:space:]]*#.*$//')"
    controller="${controller:-127.0.0.1:9090}"
    controller="${controller/0.0.0.0/127.0.0.1}"
    controller="${controller/\[::\]/127.0.0.1}"
    case "$controller" in
        :*) controller="127.0.0.1${controller}" ;;
    esac
    case "$controller" in
        http://*|https://*) ;;
        *) controller="http://${controller}" ;;
    esac
    secret="${MIHOMO_API_SECRET:-$(sed -n 's/^secret:[[:space:]]*//p' "$CONFIG_FILE" | head -n 1 | tr -d '"'"'"' \r')}"

    local curl_opts=(-sS -m 120 -o /dev/null -w '%{http_code}' -X PUT)
    [ -n "$secret" ] && curl_opts+=(-H "Authorization: Bearer ${secret}")

    local ok=0 bad=0 name encoded code
    # 不依赖 PyYAML：只扫顶层 proxy-providers: 下第一层缩进的键名
    while IFS=$'\t' read -r name encoded; do
        [ -n "$name" ] || continue
        code="$(curl "${curl_opts[@]}" "${controller%/}/providers/proxies/${encoded}" 2>/dev/null)"
        if [ "$code" = "204" ] || [ "$code" = "200" ]; then
            ok=$((ok + 1))
        else
            bad=$((bad + 1))
            echo "⚠️  订阅 ${name} 刷新失败 (HTTP ${code:-000})"
        fi
    done < <(python3 - "$CONFIG_FILE" <<'PY'
import re, sys
from urllib.parse import quote

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
start = next((i for i, l in enumerate(lines) if re.match(r"^proxy-providers\s*:\s*(#.*)?$", l)), -1)
if start < 0:
    sys.exit(0)
child_indent = None
for line in lines[start + 1:]:
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    indent = len(line) - len(line.lstrip(" "))
    if indent == 0:
        break
    if child_indent is None:
        child_indent = indent
    if indent != child_indent:
        continue
    match = re.match(r"^\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s:#][^:]*?))\s*:", line)
    if not match:
        continue
    name = next(g for g in match.groups() if g is not None).strip()
    print(f"{name}\t{quote(name, safe='')}")
PY
)
    if [ $((ok + bad)) -eq 0 ]; then
        echo "ℹ️  config.yaml 里没有 proxy-providers，跳过节点刷新。"
        return 0
    fi
    echo "🔄 节点订阅刷新完成：成功 ${ok}，失败 ${bad}"
    if [ "$bad" -gt 0 ]; then
        RESULT_MSG="${RESULT_MSG}；节点刷新 ${bad} 个失败"
        return 1
    fi
    RESULT_MSG="${RESULT_MSG}；已刷新 ${ok} 个订阅的节点"
}

if [ "$CONFIG_MODE" != "raw" ]; then
    refresh_proxy_providers || exit 1
fi
