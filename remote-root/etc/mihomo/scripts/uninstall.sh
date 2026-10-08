#!/bin/bash

# ==========================================
# Mihomo 一键卸载脚本 (完整版)
# 完整回退 install.sh 和 gateway_init.sh 留下的所有东西。
# 用法: uninstall.sh [-y] [--purge | --keep-data]
#   -y          跳过第一次确认（CLI 菜单已经确认过时使用）
#   --purge     不询问，直接删除 /etc/mihomo 数据目录
#   --keep-data 不询问，保留 /etc/mihomo
# ==========================================

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'
TMP_DIR="$(mktemp -d)"
TMP_CRON="${TMP_DIR}/crontab"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

ASSUME_YES=0
DATA_MODE="ask"
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        --purge) DATA_MODE="purge" ;;
        --keep-data) DATA_MODE="keep" ;;
    esac
done

if [ "$ASSUME_YES" -ne 1 ]; then
    echo -e "${RED}⚠️  警告：即将执行卸载操作！${NC}"
    echo "此操作将执行以下清理："
    echo "1. 停止并删除系统服务 (mihomo / mihomo-manager / force-ip-forward)"
    echo "2. 删除 /usr/bin/mihomo、/usr/bin/mihomo-core 和日志轮转配置"
    echo "3. 清理所有相关的 Crontab 自动任务 (保活/更新/看门狗)"
    echo "4. 删除网关初始化写入的 sysctl 转发配置"

    read -p "确认卸载吗？(y/n): " confirm
    if [[ "$confirm" != "y" ]]; then
        echo "已取消。"
        exit 0
    fi
fi

echo "--------------------------------"

# 1. 停止并禁用服务
echo -e "${YELLOW}[1/5] 停止系统服务...${NC}"
systemctl stop mihomo mihomo-manager force-ip-forward 2>/dev/null
systemctl disable mihomo mihomo-manager force-ip-forward 2>/dev/null

# 删除服务文件
rm -f /etc/systemd/system/mihomo.service
rm -f /etc/systemd/system/mihomo-manager.service
rm -f /etc/systemd/system/force-ip-forward.service
systemctl daemon-reload
systemctl reset-failed mihomo mihomo-manager force-ip-forward 2>/dev/null
echo "✅ 服务已移除。"

# 2. 清理 Crontab 任务
echo -e "${YELLOW}[2/5] 清理自动化任务...${NC}"
# 本项目写入 crontab 的几种标记：
#   gateway_init.sh check   -> gateway_init.sh 的保活任务
#   # MIHOMO_AUTOMATION     -> 旧版 cron_manager.sh（已删除）添加的任务，老安装可能还留着
#   # JOB_SUB / # JOB_GEO   -> 面板 app.py update_cron 添加的任务
#   /etc/mihomo/scripts/    -> 兜底，凡是调用本项目脚本的行都清掉
crontab -l 2>/dev/null \
    | grep -F -v -- "gateway_init.sh" \
    | grep -F -v -- "MIHOMO_AUTOMATION" \
    | grep -F -v -- "# JOB_SUB" \
    | grep -F -v -- "# JOB_GEO" \
    | grep -F -v -- "/etc/mihomo/scripts/" > "$TMP_CRON" || true
if [ -s "$TMP_CRON" ]; then
    crontab "$TMP_CRON"
else
    crontab -r 2>/dev/null || true
fi
echo "✅ Crontab 任务已清理。"

# 3. 删除网关初始化写入的系统配置
echo -e "${YELLOW}[3/5] 清理网关系统配置...${NC}"
rm -f /etc/sysctl.d/99-mihomo-gateway.conf
rm -f /etc/logrotate.d/mihomo
echo "✅ sysctl / logrotate 配置已删除。"

# 4. 删除程序文件
echo -e "${YELLOW}[4/5] 删除程序文件...${NC}"
rm -f /usr/bin/mihomo /usr/bin/mihomo-core /usr/bin/mihomo-core.new
echo "✅ CLI 工具和内核已删除。"

# 5. 询问是否删除数据
echo -e "${YELLOW}[5/5] 数据清理选项${NC}"
if [ "$DATA_MODE" == "ask" ]; then
    echo -e "${YELLOW}❓ 是否同时删除配置文件和数据？(/etc/mihomo)${NC}"
    echo -e "${RED}注意：删除后，你的订阅、节点、Geo数据库将全部丢失！${NC}"
    read -p "输入 'del' 确认删除数据，直接回车保留: " del_data
    if [[ "$del_data" == "del" ]]; then DATA_MODE="purge"; else DATA_MODE="keep"; fi
fi

if [[ "$DATA_MODE" == "purge" ]]; then
    echo "正在清除所有数据..."
    rm -rf /etc/mihomo
    echo "✅ 数据目录已清除。"
else
    echo "✅ 数据目录 (/etc/mihomo) 已保留。"
fi

echo "--------------------------------"
echo -e "${GREEN}卸载完成！系统已恢复干净。👋${NC}"
