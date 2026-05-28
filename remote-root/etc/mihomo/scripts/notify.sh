#!/bin/bash
# scripts/notify.sh

# 1. 引入环境变量
if [ -f "/etc/mihomo/.env" ]; then source /etc/mihomo/.env; fi

TITLE="$1"
CONTENT="$2"
# 获取当前时间
TIME_STR=$(TZ=Asia/Shanghai date "+%Y-%m-%d %H:%M:%S")
LOG_FILE="/var/log/mihomo-notify.log"

log_notify() {
    echo "[$TIME_STR] $*" >> "$LOG_FILE"
}

# --- 发送逻辑 ---

# 1. Telegram (保持不变)
if [[ "$NOTIFY_TG" == "true" && -n "$TG_BOT_TOKEN" && -n "$TG_CHAT_ID" ]]; then
    FULL_TEXT="<b>${TITLE}</b>%0A${CONTENT}%0A%0A📅 ${TIME_STR}"
    TG_CODE=$(curl -sS -m 20 -o /tmp/mihomo_notify_tg.out -w "%{http_code}" -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TG_CHAT_ID}" \
        -d text="${FULL_TEXT}" \
        -d parse_mode="HTML")
    TG_EXIT=$?
    if [[ "$TG_EXIT" -ne 0 || "$TG_CODE" -lt 200 || "$TG_CODE" -ge 300 ]]; then
        log_notify "Telegram failed title=${TITLE} exit=${TG_EXIT} http=${TG_CODE} response=$(cat /tmp/mihomo_notify_tg.out 2>/dev/null)"
    else
        log_notify "Telegram sent title=${TITLE} http=${TG_CODE}"
    fi
fi

# 2. Webhook API (修复版)
if [[ "$NOTIFY_API" == "true" && -n "$NOTIFY_API_URL" ]]; then
    # 构造正文: 内容 + 换行 + 时间
    COMBINED_MSG="${CONTENT}\n\n📅 ${TIME_STR}"
    
    # JSON 转义 (处理引号和换行)
    SAFE_TITLE=$(echo "$TITLE" | sed 's/"/\\"/g')
    # 处理正文中的换行和引号
    SAFE_MSG=$(echo "$COMBINED_MSG" | sed ':a;N;$!ba;s/\n/\\n/g' | sed 's/"/\\"/g')

    # 【核心修改】将 key 从 "message" 改为 "content" 以匹配您的模板
    API_CODE=$(curl -sS -m 20 -o /tmp/mihomo_notify_api.out -w "%{http_code}" -X POST \
        -H "Content-Type: application/json" \
        -d "{\"title\": \"${SAFE_TITLE}\", \"content\": \"${SAFE_MSG}\"}" \
        "$NOTIFY_API_URL")
    API_EXIT=$?
    if [[ "$API_EXIT" -ne 0 || "$API_CODE" -lt 200 || "$API_CODE" -ge 300 ]]; then
        log_notify "Webhook failed title=${TITLE} exit=${API_EXIT} http=${API_CODE} response=$(cat /tmp/mihomo_notify_api.out 2>/dev/null)"
    else
        log_notify "Webhook sent title=${TITLE} http=${API_CODE}"
    fi
fi
