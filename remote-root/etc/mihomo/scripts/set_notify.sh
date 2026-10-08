#!/bin/bash

# 1. 导入环境
if [ -f "/etc/mihomo/.env" ]; then
    source /etc/mihomo/.env
else
    echo "错误：未找到 .env 配置文件！"
    exit 1
fi

ENV_FILE="/etc/mihomo/.env"

upsert_env() {
    local key=$1
    local value=$2
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import os, pathlib, re, shlex, sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
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
# .env 里有密码：0600 临时文件 + 原子替换
tmp = path.with_name(f".env.{os.getpid()}.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("".join(lines))
os.replace(tmp, path)
PY
    chmod 600 "$ENV_FILE"
}

# 2. 保存函数
# notify.sh 读取的是 NOTIFY_API_URL + NOTIFY_API=true（和面板设置页一致），这里必须写同样的键
save_notify_url() {
    local url=$1
    upsert_env "NOTIFY_API_URL" "$url"
    upsert_env "NOTIFY_API" "true"
    # 刷新变量
    source "$ENV_FILE"
    echo "✅ 通知地址已保存！"
}

# 3. 交互菜单
echo "==================================="
echo "       通知接口配置"
echo "==================================="
echo "当前地址: ${NOTIFY_API_URL:-未设置} (开关: ${NOTIFY_API:-false})"
echo "-----------------------------------"
echo "1. 设置/修改 通知地址"
echo "2. 发送测试消息"
echo "3. 清空通知地址 (关闭通知)"
echo "0. 返回主菜单"
echo "==================================="
read -p "请选择: " choice

case $choice in
    1)
        read -p "请输入新的通知接口 URL: " input_url
        if [ -z "$input_url" ]; then
            echo "输入为空，取消操作。"
        else
            save_notify_url "$input_url"
            
            # 顺便问一句要不要测试
            read -p "设置完成，是否立即发送一条测试消息？(y/n): " test_choice
            if [ "$test_choice" == "y" ]; then
                bash ${SCRIPT_PATH}/notify.sh "测试" "这是一条来自 Mihomo 的测试消息"
            fi
        fi
        ;;
    2)
        if [ -z "$NOTIFY_API_URL" ]; then
            echo "❌ 错误：尚未设置地址，请先选择 [1] 进行设置。"
        else
            echo "正在发送测试消息..."
            bash ${SCRIPT_PATH}/notify.sh "测试" "这是一条手动触发的测试消息"
            echo "发送指令已执行，请检查接收端。"
        fi
        ;;
    3)
        # 清空逻辑
        upsert_env "NOTIFY_API_URL" ""
        upsert_env "NOTIFY_API" "false"
        echo "通知地址已清空，通知功能已关闭。"
        ;;
    0)
        exit 0
        ;;
    *)
        echo "无效选项"
        ;;
esac
