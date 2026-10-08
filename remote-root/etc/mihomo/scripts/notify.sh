#!/bin/bash
# scripts/notify.sh

# 1. 引入环境变量
if [ -f "/etc/mihomo/.env" ]; then source /etc/mihomo/.env; fi

TITLE="$1"
CONTENT="$2"
# 获取当前时间
TIME_STR=$(TZ=Asia/Shanghai date "+%Y-%m-%d %H:%M:%S")
LOG_FILE="/var/log/mihomo-notify.log"
TMP_DIR="$(mktemp -d)"
TG_OUT="${TMP_DIR}/mihomo_notify_tg.out"
API_OUT="${TMP_DIR}/mihomo_notify_api.out"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log_notify() {
    echo "[$TIME_STR] $*" >> "$LOG_FILE"
}

# 这个脚本的 stdout 会原样回显到 Web 面板（"测试通知"按钮），所以：
#   - curl 的错误输出一律写到临时文件，不直接打到 stdout/stderr（curl 报错时会把 URL 连同 bot token 一起打印出来）
#   - Telegram 的 token 通过 curl 配置 (-K -) 从 stdin 传入，不出现在命令行参数里
SUMMARY=()

# JSON 转义：先转义反斜杠，再转义引号，否则 \" 会被二次转义坏掉；控制字符也要处理
json_escape() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.argv[1], ensure_ascii=False)[1:-1])' "$1"
    else
        printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' -e 's/\r/\\r/g' | sed ':a;N;$!ba;s/\n/\\n/g'
    fi
}

# --- 发送逻辑 ---

# 1. Telegram
if [[ "$NOTIFY_TG" == "true" && -n "$TG_BOT_TOKEN" && -n "$TG_CHAT_ID" ]]; then
    FULL_TEXT="<b>${TITLE}</b>%0A${CONTENT}%0A%0A📅 ${TIME_STR}"
    TG_CODE=$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TG_BOT_TOKEN" \
        | curl -sS -m 20 -K - -o "$TG_OUT" -w "%{http_code}" -X POST \
            -d chat_id="${TG_CHAT_ID}" \
            -d text="${FULL_TEXT}" \
            -d parse_mode="HTML" 2>"${TMP_DIR}/tg.err")
    TG_EXIT=$?
    if [[ "$TG_EXIT" -ne 0 || "$TG_CODE" -lt 200 || "$TG_CODE" -ge 300 ]]; then
        # 日志里也把 token 抹掉
        TG_ERR=$(sed "s#${TG_BOT_TOKEN}#***#g" "${TMP_DIR}/tg.err" 2>/dev/null | tr '\n' ' ')
        log_notify "Telegram failed title=${TITLE} exit=${TG_EXIT} http=${TG_CODE} error=${TG_ERR} response=$(cat "$TG_OUT" 2>/dev/null)"
        SUMMARY+=("Telegram: 发送失败 (exit=${TG_EXIT} http=${TG_CODE})")
    else
        log_notify "Telegram sent title=${TITLE} http=${TG_CODE}"
        SUMMARY+=("Telegram: 已发送 (http=${TG_CODE})")
    fi
fi

# 2. Webhook API
if [[ "$NOTIFY_API" == "true" && -n "$NOTIFY_API_URL" ]]; then
    # 构造正文: 内容 + 空行 + 时间（真正的换行，由 json_escape 转成 \n）
    COMBINED_MSG="${CONTENT}"$'\n\n'"📅 ${TIME_STR}"
    SAFE_TITLE=$(json_escape "$TITLE")
    SAFE_MSG=$(json_escape "$COMBINED_MSG")

    # key 用 "content" 以匹配常见的 webhook 模板
    API_CODE=$(curl -sS -m 20 -o "$API_OUT" -w "%{http_code}" -X POST \
        -H "Content-Type: application/json" \
        -d "{\"title\": \"${SAFE_TITLE}\", \"content\": \"${SAFE_MSG}\"}" \
        "$NOTIFY_API_URL" 2>"${TMP_DIR}/api.err")
    API_EXIT=$?
    if [[ "$API_EXIT" -ne 0 || "$API_CODE" -lt 200 || "$API_CODE" -ge 300 ]]; then
        log_notify "Webhook failed title=${TITLE} exit=${API_EXIT} http=${API_CODE} error=$(tr '\n' ' ' < "${TMP_DIR}/api.err" 2>/dev/null) response=$(cat "$API_OUT" 2>/dev/null)"
        SUMMARY+=("Webhook: 发送失败 (exit=${API_EXIT} http=${API_CODE})")
    else
        log_notify "Webhook sent title=${TITLE} http=${API_CODE}"
        SUMMARY+=("Webhook: 已发送 (http=${API_CODE})")
    fi
fi

if [ "${#SUMMARY[@]}" -eq 0 ]; then
    echo "未启用任何通知渠道（NOTIFY_API / NOTIFY_TG 都未开启）。"
else
    printf '%s\n' "${SUMMARY[@]}"
fi
