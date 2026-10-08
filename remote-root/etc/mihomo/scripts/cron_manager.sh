#!/bin/bash

if [ -f "/etc/mihomo/.env" ]; then
    source /etc/mihomo/.env
else
    echo "错误：未找到 .env 配置文件！"
    exit 1
fi

SCRIPT_PATH="${SCRIPT_PATH:-/etc/mihomo/scripts}"
JOB_ID="# MIHOMO_AUTOMATION"
TMP_DIR="$(mktemp -d)"
TMP_CRON="${TMP_DIR}/crontab"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

current_cron() {
    crontab -l 2>/dev/null || true
}

remove_matching_cron() {
    local pattern=$1
    current_cron | grep -F -v -- "$pattern" > "$TMP_CRON" || true
    crontab "$TMP_CRON"
}

# --- 升级版添加函数 (支持覆盖修改) ---
add_cron() {
    local schedule=$1
    local script=$2
    local comment=$3
    
    # 1. 先清理旧的同名任务 (根据注释匹配)
    # 逻辑：列出所有任务 -> 过滤掉含当前注释的行 -> 重新写回 crontab
    current_cron | grep -F -v -- "$comment" > "$TMP_CRON" || true
    
    # 2. 添加新任务
    # 逻辑：列出 -> 追加新行 -> 写回
    echo "$schedule /bin/bash $script $JOB_ID - $comment" >> "$TMP_CRON"
    crontab "$TMP_CRON"
    echo "✅ 任务设置成功: $comment"
    echo "   执行时间: $schedule"
}

remove_cron() {
    remove_matching_cron "$JOB_ID"
    echo "🗑️  已清理所有 Mihomo 相关的自动任务。"
}

echo "==================================="
echo "   Mihomo 自动化任务管理"
echo "==================================="
echo "1. 设置 [故障自愈看门狗]"
echo "2. 设置 [Geo 数据库自动更新]"
echo "3. 设置 [订阅配置自动更新] (需先配置订阅)"
echo "4. 清空所有任务"
echo "0. 返回"
echo "==================================="
read -p "请选择: " choice

case $choice in
    1)
        # 看门狗通常不需要改时间，默认 5 分钟即可
        add_cron "*/5 * * * *" "${SCRIPT_PATH}/watchdog.sh" "故障自愈看门狗"
        ;;
    2)
        read -p "请输入更新时间 (Cron格式，默认 0 4 * * * 即凌晨4点): " t_geo
        if [ -z "$t_geo" ]; then t_geo="0 4 * * *"; fi
        add_cron "$t_geo" "${SCRIPT_PATH}/update_geo.sh" "Geo数据库更新"
        ;;
    3)
        # 检查是否已保存订阅链接（.env 里的键是 SUB_URL_RAW / SUB_URL_AIRPORT，由 CONFIG_MODE 决定用哪个）
        if [ "$CONFIG_MODE" == "raw" ]; then
            CURRENT_SUB="$SUB_URL_RAW"
        else
            CURRENT_SUB="$SUB_URL_AIRPORT"
        fi
        if [ -z "$CURRENT_SUB" ]; then
            echo -e "\n⚠️  错误：系统中未找到已保存的订阅链接！"
            echo "请先去 [菜单 3] -> [粘贴订阅链接]，或在 Web 面板的订阅页保存链接。"
            exit 1
        fi

        echo "当前模式: ${CONFIG_MODE:-airport}"
        echo "当前订阅链接: $CURRENT_SUB"
        read -p "请输入更新时间 (Cron格式，默认 0 5 * * * 即凌晨5点): " t_sub
        if [ -z "$t_sub" ]; then t_sub="0 5 * * *"; fi

        # update_subscription.sh 自己读 .env 里的 CONFIG_MODE / SUB_URL_*，不需要带参数
        add_cron "$t_sub" "${SCRIPT_PATH}/update_subscription.sh" "订阅自动更新"
        ;;
    4)
        remove_cron
        ;;
    0)
        exit 0
        ;;
    *)
        echo "无效选项"
        ;;
esac
