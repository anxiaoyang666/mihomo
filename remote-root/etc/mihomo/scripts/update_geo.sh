#!/bin/bash
# update_geo.sh - Geo 数据更新（面板「更新 Geo 数据库」按钮 / # JOB_GEO 定时任务 / CLI 菜单 6）
#
# 这个任务到底刷新什么：
#   1. 自带模板 templates/default.yaml 的规则全部走 rule-providers（.mrs / .list），由内核按 interval 自己定期
#      拉取；它既不用 geoip.dat/geosite.dat，也没有配置 geodata-mode / geox-url。所以对模板生成的配置来说，
#      这里要做的是让正在运行的内核立刻重新拉一遍 config.yaml 里声明的所有 rule-providers
#      （控制器 API：PUT /providers/rules/<name>，不需要重启）。
#   2. 「配置托管」模式下用户自己的 config.yaml 可能写了 GEOIP / GEOSITE 规则。内核没配 geox-url 时的默认下载源是
#      MetaCubeX/meta-rules-dat 的 latest release：GEOSITE 用 geosite.dat，GEOIP 用 geoip.metadb（geodata-mode: false，
#      默认）或 geoip.dat（geodata-mode: true）。这里把这三个文件下载到临时目录，只有内容真的变了才替换，
#      并且只有替换了至少一个文件才重启内核（这些文件内核只在启动时加载）。
#   3. 三个文件全部下载失败：发通知并以非 0 退出，不重启。

MIHOMO_DIR="${MIHOMO_DIR:-/etc/mihomo}"
GEO_DIR="${MIHOMO_DIR}"
ENV_FILE="${MIHOMO_DIR}/.env"
CONFIG_FILE="${MIHOMO_DIR}/config.yaml"
NOTIFY_SCRIPT="${MIHOMO_DIR}/scripts/notify.sh"
TMP_DIR="$(mktemp -d)"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ -f "$ENV_FILE" ]; then source "$ENV_FILE"; fi

# TLS 默认严格校验；只有 .env 里显式写 ALLOW_INSECURE_TLS=true 才跳过证书检查
# 三个文件最坏情况 3 x 2 x 20s，留在面板 180 秒的执行上限之内
WGET_OPTS=(--timeout=20 --tries=2)
if [ "$ALLOW_INSECURE_TLS" == "true" ]; then
    WGET_OPTS+=(--no-check-certificate)
fi

GEO_BASE_URL="https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest"
GEO_FILES=(geoip.dat geosite.dat geoip.metadb)

notify() {
    [ -x "$NOTIFY_SCRIPT" ] || [ -f "$NOTIFY_SCRIPT" ] || return 0
    bash "$NOTIFY_SCRIPT" "$1" "$2" >/dev/null 2>&1 || true
}

echo "⬇️  开始更新 Geo 数据..."

# ==========================================
# 1. Geo 数据文件：下载到临时目录，内容变了才替换
# ==========================================
replaced=0
failed=0
for name in "${GEO_FILES[@]}"; do
    tmp_file="${TMP_DIR}/${name}"
    target="${GEO_DIR}/${name}"
    if wget "${WGET_OPTS[@]}" -O "$tmp_file" "${GEO_BASE_URL}/${name}" >/dev/null 2>&1 && [ -s "$tmp_file" ]; then
        if [ -f "$target" ] && cmp -s "$tmp_file" "$target"; then
            echo "✅ ${name} 已是最新"
        else
            mv -f "$tmp_file" "$target"
            echo "✅ ${name} 已更新"
            replaced=$((replaced + 1))
        fi
    else
        echo "❌ ${name} 下载失败"
        failed=$((failed + 1))
    fi
done

if [ "$failed" -eq "${#GEO_FILES[@]}" ]; then
    echo "❌ 所有 Geo 数据文件都下载失败，未改动任何文件，也不重启内核。"
    notify "❌ Geo 更新失败" "geoip.dat / geosite.dat / geoip.metadb 全部下载失败，请检查网络或 GH 代理。"
    exit 1
fi

# ==========================================
# 2. 让内核重新拉取 config.yaml 里的 rule-providers（不重启）
# ==========================================
refresh_rule_providers() {
    [ -f "$CONFIG_FILE" ] || return 0
    systemctl is-active --quiet mihomo || { echo "ℹ️  内核未运行，跳过规则集刷新。"; return 0; }

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

    local curl_opts=(-sS -m 60 -o /dev/null -w '%{http_code}' -X PUT)
    [ -n "$secret" ] && curl_opts+=(-H "Authorization: Bearer ${secret}")

    local ok=0 bad=0 line name encoded code
    # 不依赖 PyYAML：只扫顶层 rule-providers: 下第一层缩进的键名
    while IFS=$'\t' read -r name encoded; do
        [ -n "$name" ] || continue
        code="$(curl "${curl_opts[@]}" "${controller%/}/providers/rules/${encoded}" 2>/dev/null)"
        if [ "$code" = "204" ] || [ "$code" = "200" ]; then
            ok=$((ok + 1))
        else
            bad=$((bad + 1))
            echo "⚠️  规则集 ${name} 刷新失败 (HTTP ${code:-000})"
        fi
    done < <(python3 - "$CONFIG_FILE" <<'PY'
import re, sys
from urllib.parse import quote

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
start = next((i for i, l in enumerate(lines) if re.match(r"^rule-providers\s*:\s*(#.*)?$", l)), -1)
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
        echo "ℹ️  config.yaml 里没有 rule-providers，跳过规则集刷新。"
    else
        echo "🔄 规则集刷新完成：成功 ${ok}，失败 ${bad}"
    fi
}
refresh_rule_providers

# ==========================================
# 3. 只有 Geo 文件真的换了才重启（内核只在启动时加载 .dat/.metadb）
# ==========================================
if [ "$replaced" -gt 0 ]; then
    echo "♻️  ${replaced} 个 Geo 文件有更新，正在重启内核..."
    systemctl restart mihomo
else
    echo "✅ Geo 文件无变化，不重启内核。"
fi
if [ "$failed" -gt 0 ]; then
    echo "⚠️  有 ${failed} 个文件下载失败，已保留旧文件。"
fi
echo "🏁 Geo 更新任务结束"
