#!/bin/bash
# update_subscription.sh - 订阅更新 (支持 Raw/Airport + 自动防回环注入 + 优雅通知)

MIHOMO_DIR="/etc/mihomo"
ENV_FILE="${MIHOMO_DIR}/.env"
CONFIG_FILE="${MIHOMO_DIR}/config.yaml"
TEMPLATE_FILE="${MIHOMO_DIR}/templates/default.yaml"
BACKUP_DIR="${MIHOMO_DIR}/backup"
NOTIFY_SCRIPT="${MIHOMO_DIR}/scripts/notify.sh"
# 通知统一走 notify.sh --event：标题 "{图标} {站点名} · {主题}"，正文一行一个事实
notify_event() {
    bash "$NOTIFY_SCRIPT" --event "$@" >/dev/null 2>&1 || true
}
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
        notify_event warn "订阅配置下载失败" "托管配置链接下载不下来（网络不通或链接已失效）" "配置未变化，代理继续使用当前配置"
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
        notify_event warn "订阅配置生成失败" "按模板生成新配置时出错（模板文件或机场链接有问题）" "配置未变化，代理继续使用当前配置"
        rm -f "$TEMP_NEW"
        exit 1
    fi
fi

# ==========================================
# 第二阶段：通用补丁 (注入防回环规则)
# ==========================================

# 四个站点共用一份托管配置，里面有 IP-CIDR,<各站点网段>,Home-xx 规则（经隧道去别的站点）；
# 本站点自己的网段必须走 DIRECT 并从 TUN 排除，否则访问本地局域网会绕进指向自己的隧道。
# LOCAL_CIDR 可以是逗号分隔的多个网段；留空时自动识别本机网段（和面板 app.effective_local_networks 同一套规则）。
echo "🛡️ 正在注入本地直连网段（防回环）..."
export LOCAL_CIDR
python3 - "$TEMP_NEW" "${MIHOMO_DIR}/manager" <<'PY'
import ipaddress, os, re, subprocess, sys

config_path, manager_dir = sys.argv[1], sys.argv[2]

# BEGIN_LOCAL_NETWORKS_FALLBACK
# 面板 app.py 加载不了时（缺 Flask 等）用的同款实现，规则必须和 app.normalize_local_networks 保持一致（有测试比对）
LOCAL_NETWORKS_MAX = 8
_ALLOWED_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
_ALLOWED_V6 = (ipaddress.ip_network("fc00::/7"),)
_TUN_V4 = ipaddress.ip_network("198.18.0.0/15")

def _parse_local_network(raw):
    shown = raw if len(raw) <= 40 else raw[:40] + "…"
    bad = f"无法识别「{shown}」，请填写形如 10.10.20.0 或 10.10.20.0/24 的网段"
    try:
        if "/" in raw:
            net = ipaddress.ip_network(raw, strict=False)
        elif re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", raw):
            net = ipaddress.ip_network(raw + ".0/24")
        else:
            addr = ipaddress.ip_address(raw)
            net = ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 64}", strict=False)
    except ValueError:
        return None, bad
    if net.version == 4:
        if net.prefixlen < 8:
            return None, f"「{net}」范围太大，IPv4 网段至少要 /8"
        allowed = _ALLOWED_V4
    else:
        if net.prefixlen < 32:
            return None, f"「{net}」范围太大，IPv6 网段至少要 /32"
        allowed = _ALLOWED_V6
    if not any(net.subnet_of(a) for a in allowed):
        return None, (f"「{net}」不是局域网地址，只能填写局域网网段"
                      "（10.x、172.16-31.x、192.168.x、100.64-127.x 或 IPv6 fd00::/8）")
    return net, None

def normalize_local_networks(text):
    if text is None:
        return [], None
    if isinstance(text, (list, tuple)):
        text = ",".join(str(item) for item in text)
    networks = []
    for raw in re.split(r"[\s,，、;；]+", str(text)):
        if not raw:
            continue
        net, error = _parse_local_network(raw)
        if error:
            return [], error
        if str(net) not in networks:
            networks.append(str(net))
    if len(networks) > LOCAL_NETWORKS_MAX:
        return [], f"最多填写 {LOCAL_NETWORKS_MAX} 个网段（现在有 {len(networks)} 个）"
    return networks, None

def parse_ip_addr_networks(output):
    networks = []
    for line in str(output or "").splitlines():
        match = re.match(r"^\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not match:
            continue
        ifname = match.group(1).split("@")[0].lower()
        if "meta" in ifname or ifname.startswith(("tun", "utun")):
            continue
        try:
            iface = ipaddress.ip_interface(match.group(2))
        except ValueError:
            continue
        if iface.ip in _TUN_V4:
            continue
        net, error = _parse_local_network(str(iface.network))
        if error or str(net) in networks:
            continue
        networks.append(str(net))
    return networks[:LOCAL_NETWORKS_MAX]

def detect_local_networks():
    try:
        result = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=5)
        return parse_ip_addr_networks(result.stdout) if result.returncode == 0 else []
    except Exception:
        return []

def effective_local_networks(stored, detect=None):
    networks, error = normalize_local_networks(stored)
    if networks and not error:
        return networks, False, None
    return list((detect or detect_local_networks)()), True, error
# END_LOCAL_NETWORKS_FALLBACK

try:
    sys.path.insert(0, manager_dir)
    import app as panel
    effective_local_networks = panel.effective_local_networks
except Exception as e:
    print(f"ℹ️ 未加载面板模块，使用脚本内置的网段识别: {e}")

try:
    import yaml
    networks, auto, error = effective_local_networks(os.environ.get('LOCAL_CIDR', ''))
    if error:
        print(f"⚠️ .env 里的 LOCAL_CIDR 无效（{error}），改为自动识别本机网段")
    if not networks:
        print("⚠️ 没有可用的本地直连网段（LOCAL_CIDR 为空且没识别到本机局域网网段），跳过防回环注入")
        sys.exit(0)
    print(f"🛡️ 本地直连网段{'（自动识别）' if auto else ''}: {', '.join(networks)}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    # 规则：IP-CIDR,10.10.20.0/24,DIRECT,no-resolve；IPv6 用 IP-CIDR6。按填写顺序放在最前面
    loop_rules = [
        f"{'IP-CIDR6' if ipaddress.ip_network(net).version == 6 else 'IP-CIDR'},{net},DIRECT,no-resolve"
        for net in networks
    ]

    # 同步 TUN 路由排除，避免本地网段被 auto-route 接管
    if not isinstance(config.get('tun'), dict):
        config['tun'] = {}
    route_exclude = config['tun'].get('route-exclude-address')
    if not isinstance(route_exclude, list):
        route_exclude = []
    config['tun']['route-exclude-address'] = list(networks) + [c for c in route_exclude if c not in networks]
    print(f"✅ 已同步 TUN 路由排除: {', '.join(networks)}")

    rules = config.get('rules')
    if not isinstance(rules, list):
        rules = []
    config['rules'] = loop_rules + [r for r in rules if r not in loop_rules]
    for rule in loop_rules:
        print(f"✅ 已插入规则: {rule}")

    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)

except Exception as e:
    # 不退出：主体配置可能仍然可用，后面还有内核校验；日志里留警告
    print(f"⚠️ 防回环注入失败: {e}")
PY

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
    notify_event warn "订阅配置无效" "生成的新配置是空的" "已保留旧配置，代理继续正常工作"
    exit 1
fi

# 应用前先用内核校验，坏配置（比如订阅过期返回的 HTML 页面）不能覆盖正在用的配置
CORE_BIN="/usr/bin/mihomo-core"
if [ -x "$CORE_BIN" ]; then
    if ! CHECK_OUT="$("$CORE_BIN" -t -d "$MIHOMO_DIR" -f "$TEMP_NEW" 2>&1)"; then
        echo "❌ 新配置校验失败，已保留当前配置。"
        echo "$CHECK_OUT" | tail -n 5
        RESULT_MSG="新配置校验失败，已保留当前配置"
        notify_event warn "订阅配置无效" "新配置没通过 mihomo 校验（订阅可能返回了错误页面或已过期）" "已保留旧配置，代理继续正常工作"
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
    # 留一份旧内容给面板比较：没碰 TUN/端口等网络结构就热加载，不断开连接
    OLD_COPY="${TMP_DIR}/config_old.yaml"
    if [ -f "$CONFIG_FILE" ]; then cp "$CONFIG_FILE" "$OLD_COPY"; else : > "$OLD_COPY"; fi
    mv "$TEMP_NEW" "$CONFIG_FILE"

    APPLY_ACTION="restarted"
    if [ -f "${MIHOMO_DIR}/manager/app.py" ]; then
        # 由面板的 apply_config_change 决定热加载还是重启（热加载失败会自动改为重启）；stdout 只输出动作
        APPLY_ACTION="$(python3 - "$OLD_COPY" "$CONFIG_FILE" <<'PY'
import subprocess, sys
sys.path.insert(0, "/etc/mihomo/manager")
old_path, new_path = sys.argv[1], sys.argv[2]
try:
    import app
except Exception as e:
    print(f"⚠️ 无法加载面板，直接重启: {e}", file=sys.stderr)
    ok = subprocess.run(["systemctl", "restart", "mihomo"]).returncode == 0
    print("restarted")
    sys.exit(0 if ok else 1)
with open(old_path, encoding="utf-8") as f:
    old_text = f.read()
with open(new_path, encoding="utf-8") as f:
    new_text = f.read()
ok, message, action = app.apply_config_change(old_text, new_text)
print(message, file=sys.stderr)
# 给通知用的一句话：为什么要重启
why = ""
if action == "restarted":
    needs_restart, reasons = app.config_needs_restart(old_text, new_text)
    keys = [r[len("修改了 "):] for r in reasons if r.startswith("修改了 ")]
    if keys:
        why = "改动了 " + "、".join(keys[:4]) + ("等" if len(keys) > 4 else "") + "，需要重启"
    elif needs_restart:
        why = "无法比较新旧配置，保守起见重启"
    else:
        why = "热加载没成功，改为重启"
print(f"{action}\t{why}")
sys.exit(0 if ok else 1)
PY
)"
        APPLY_RC=$?
        APPLY_LINE="$(printf '%s\n' "$APPLY_ACTION" | tail -n 1)"
        APPLY_ACTION="${APPLY_LINE%%$'\t'*}"
        APPLY_WHY=""
        [ "$APPLY_LINE" != "$APPLY_ACTION" ] && APPLY_WHY="${APPLY_LINE#*$'\t'}"
    else
        systemctl restart mihomo
        APPLY_RC=$?
        APPLY_WHY=""
    fi
    # 应用失败或应用后没在运行就回滚，不然 Restart=always 会让网关一直崩溃循环
    APPLY_FAILED=0
    if [ "$APPLY_RC" -ne 0 ]; then
        APPLY_FAILED=1
    else
        sleep 3
        systemctl is-active --quiet mihomo || APPLY_FAILED=1
    fi
    if [ "$APPLY_FAILED" -eq 1 ]; then
        echo "❌ 新配置应用失败，正在回滚到更新前的配置..."
        if [ -f "$BACKUP_FILE" ]; then
            cp "$BACKUP_FILE" "$CONFIG_FILE"
            systemctl restart mihomo
            sleep 3
            if systemctl is-active --quiet mihomo; then
                ROLLBACK_LINE="已恢复更新前的配置，mihomo 运行正常"
            else
                ROLLBACK_LINE="已恢复更新前的配置，但 mihomo 仍未运行，需要人工处理"
            fi
        else
            ROLLBACK_LINE="没有可恢复的旧配置，需要人工处理"
        fi
        RESULT_MSG="新配置启动失败，已回滚"
        notify_event fail "订阅配置应用失败，已回滚" "新配置让 mihomo 启动失败" "$ROLLBACK_LINE"
        exit 1
    fi
    if [ "$APPLY_ACTION" = "reloaded" ]; then
        echo "🎉 更新完成并热加载（未中断连接）。"
        RESULT_MSG="配置已更新并热加载（未中断连接）"
        APPLY_LINE="已热加载，现有连接没有中断"
    else
        echo "🎉 更新完成并重启。"
        RESULT_MSG="配置已更新并重启"
        APPLY_LINE="已重启 mihomo${APPLY_WHY:+：${APPLY_WHY}}"
    fi

    if [ "$CONFIG_MODE" == "raw" ]; then
        SOURCE_LINE="来源：托管配置"
    else
        SOURCE_LINE="来源：机场节点订阅"
    fi
    notify_event ok "订阅配置已更新" "$APPLY_LINE" "$SOURCE_LINE"
else
    rm -f "$TEMP_NEW"
    RESULT_MSG="配置无变更"
fi

# ==========================================
# 第五阶段：让内核立刻重新拉取节点
# ==========================================

# 不管是机场模式还是托管模式，只要 config.yaml 里有 proxy-providers，节点列表就是内核按 interval
# 自己去拉的，上面经常是"配置无变更"。要让"每天更新订阅"真的更新节点，得通过控制器 API 让内核立刻刷新：
#   PUT /providers/proxies/<name>
# 重启也不会触发刷新（内核会直接用缓存的 providers/*.yaml），所以无论配置有没有变都执行。
# 刷新失败（典型：机场订阅地址 404）会把原因写进 .last_subscription，概览页能直接看到。
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

    local ok=0 bad=0 name encoded code first_error="" failed_lines=()
    # 不依赖 PyYAML：只扫顶层 proxy-providers: 下第一层缩进的键名
    while IFS=$'\t' read -r name encoded; do
        [ -n "$name" ] || continue
        code="$(curl "${curl_opts[@]}" "${controller%/}/providers/proxies/${encoded}" 2>/dev/null)"
        if [ "$code" = "204" ] || [ "$code" = "200" ]; then
            ok=$((ok + 1))
        else
            bad=$((bad + 1))
            # 内核把上游的错误原样转成状态码（机场 404 → 这里也是 404），把它带到结果里
            echo "⚠️  订阅 ${name} 刷新失败 (HTTP ${code:-000})"
            [ -n "$first_error" ] || first_error="${name} HTTP ${code:-000}"
            if [ "${code:-000}" = "000" ]; then
                failed_lines+=("机场「${name}」没有响应（超时或连不上），订阅链接可能已失效或机场故障")
            else
                failed_lines+=("机场「${name}」返回 HTTP ${code}，订阅链接可能已失效或机场故障")
            fi
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
        RESULT_MSG="${RESULT_MSG}；节点刷新失败 ${bad} 个（${first_error}），请检查机场订阅地址"
        # 同一个问题每天都会失败：notify.sh 按 key 去重（首次发、之后每 3 天提醒一次、恢复时发一次）
        local lines=("${failed_lines[@]:0:2}")
        [ "$bad" -gt 2 ] && lines+=("另有 $((bad - 2)) 个机场也刷新失败")
        lines+=("代理继续使用上次拿到的旧节点")
        notify_event warn --key sub_providers "订阅节点刷新失败" "${lines[@]}"
        return 1
    fi
    RESULT_MSG="${RESULT_MSG}；已刷新 ${ok} 个订阅的节点"
    notify_event ok --key sub_providers "订阅节点刷新已恢复" "${ok} 个机场订阅的节点都已正常刷新"
}

refresh_proxy_providers || exit 1
