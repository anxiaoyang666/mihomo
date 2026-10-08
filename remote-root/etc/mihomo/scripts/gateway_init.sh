#!/bin/bash

# ==========================================
# Mihomo 网关网络初始化脚本 (智能持久化版)
# ==========================================

# 模式: check (每分钟 cron 静默保活) / init 或不带参数 (首次运行/手动执行)
MODE="$1"

# 1. 环境加载
if [ -f "/etc/mihomo/.env" ]; then source /etc/mihomo/.env; fi

# .env 里 GATEWAY_AUTOFIX=false 时，cron 的 check 什么都不做（手动执行仍然生效）。
# 适合自己管理 iptables/sysctl、不希望每分钟被改回去的场景。放在最前面，连网卡探测都不做。
if [ "$MODE" == "check" ] && [ "${GATEWAY_AUTOFIX:-true}" == "false" ]; then
    exit 0
fi

# 兜底路径 (防止 .env 不存在或变量缺失)
SCRIPT_PATH="${SCRIPT_PATH:-/etc/mihomo/scripts}"
CURRENT_SCRIPT="${SCRIPT_PATH}/gateway_init.sh"
SYSCTL_FILE="/etc/sysctl.d/99-mihomo-gateway.conf"
TMP_DIR="$(mktemp -d)"
TMP_CRON="${TMP_DIR}/gateway_crontab"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log() {
    # 只有非 check 模式才输出日志，避免 cron 邮件轰炸
    if [ "$MODE" != "check" ]; then
        echo -e "$1"
    fi
}

# 2. 自动识别网卡
IFACE=$(ip route show default | awk '/default/ {print $5}' | head -n 1)
if [ -z "$IFACE" ]; then
    IFACE="eth0" # 兜底
fi

# ==========================================
# 核心功能：规则检测与应用
# 每一步都先读当前值、不一样才写：check 模式每分钟跑一次，不能每次都重写 sysctl / iptables。
# ==========================================

# 持久化文件：ip_forward 和 rp_filter 一起写进去，重启后 sysctl 自己恢复，不用等 cron 修
ensure_sysctl_file() {
    local desired
    desired="$(printf 'net.ipv4.ip_forward=1\nnet.ipv4.conf.all.rp_filter=0\nnet.ipv4.conf.default.rp_filter=0\n')"
    if [ ! -f "$SYSCTL_FILE" ] || [ "$(cat "$SYSCTL_FILE" 2>/dev/null)" != "$desired" ]; then
        printf '%s\n' "$desired" > "$SYSCTL_FILE"
        return 0
    fi
    return 1
}

apply_rules() {
    local changed=0

    # A. 开启内核转发
    # --------------------------------------
    # 读取当前状态
    local ip_fwd=$(sysctl -n net.ipv4.ip_forward 2>/dev/null)
    if [ "$ip_fwd" != "1" ]; then
        sysctl -w net.ipv4.ip_forward=1 > /dev/null
        log "✅ 内核转发已开启"
        changed=1
    fi
    if ensure_sysctl_file; then
        log "✅ 已写入持久化配置 ${SYSCTL_FILE}"
        changed=1
    fi

    # B. 基础防火墙策略 (FORWARD)
    # --------------------------------------
    # 确保 FORWARD 链策略是 ACCEPT (关键！)
    # 注意：我们不再暴力 Flush 所有规则，以免误伤 Docker
    # 检查和修复必须针对同一个东西：修复设的是链的默认策略 (-P)，
    # 所以检查也要看策略，而不是用 -C 找一条 "-j ACCEPT" 规则（那条规则从来不存在，会导致每分钟都"修复"一次）
    if ! iptables -S FORWARD 2>/dev/null | grep -q '^-P FORWARD ACCEPT'; then
        iptables -P FORWARD ACCEPT
        log "✅ FORWARD 默认策略已设为 ACCEPT"
        changed=1
    fi

    # C. NAT 伪装 (Masquerade)
    # --------------------------------------
    # 检查是否已有针对该出口网卡的 MASQUERADE 规则
    iptables -t nat -C POSTROUTING -o "$IFACE" -j MASQUERADE 2>/dev/null
    if [ $? -ne 0 ]; then
        iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE
        log "✅ NAT 伪装规则已添加 -> $IFACE"
        changed=1
    fi

    # D. 关闭反向路径过滤 (RP_Filter)
    # --------------------------------------
    # 新插的网卡会继承 conf/default 的值，所以每个接口都要看一眼；但只有值不是 0 的才写
    local rp_changed=0
    for i in /proc/sys/net/ipv4/conf/*/rp_filter; do
        [ -f "$i" ] || continue
        if [ "$(cat "$i" 2>/dev/null)" != "0" ]; then
            echo 0 > "$i"
            rp_changed=1
        fi
    done
    if [ $rp_changed -eq 1 ]; then
        log "✅ 路径过滤限制已放宽 (RP_Filter)"
        changed=1
    fi

    # 结果反馈
    if [ $changed -eq 1 ]; then
        log "${GREEN}>>> 网关规则已修复/初始化完成。${NC}"
    else
        log "${GREEN}>>> 网关规则正常，无需变更。${NC}"
    fi
}

# ==========================================
# 自动持久化 (Auto-Persistence)
# ==========================================
ensure_cron() {
    # 检查 Crontab 中是否已有本脚本
    # 这里的 grep 查找 "gateway_init.sh check"
    if ! crontab -l 2>/dev/null | grep -qF "gateway_init.sh check"; then
        log "${YELLOW}正在添加自动保活任务 (Crontab)...${NC}"
        
        # 添加每分钟执行一次 check
        crontab -l 2>/dev/null | grep -F -v -- "gateway_init.sh check" > "$TMP_CRON" || true
        echo "*/1 * * * * /bin/bash ${CURRENT_SCRIPT} check >/dev/null 2>&1" >> "$TMP_CRON"
        crontab "$TMP_CRON"
        
        log "✅ 保活任务已添加。即使防火墙被重置，1分钟内将自动恢复。"
    fi
}

# ==========================================
# 主流程
# ==========================================

apply_rules

# 仅在非 check 模式下(即手动执行或服务启动时)检查 cron
# 避免 cron 任务自己无限递归检查自己
if [ "$MODE" != "check" ]; then
    ensure_cron
fi
