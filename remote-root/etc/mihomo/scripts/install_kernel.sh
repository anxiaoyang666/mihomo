#!/bin/bash
# install_kernel.sh - 下载并安装 mihomo 内核到 /usr/bin/mihomo-core
# 用法: install_kernel.sh [auto]   (auto = 不询问，直接装最新版；CLI 和 install.sh 都走这个模式)

# 1. 加载环境
if [ -f "/etc/mihomo/.env" ]; then source /etc/mihomo/.env; fi
# .env 缺失或没写 MIHOMO_PATH 时的兜底，避免 mv 到 "/mihomo"
MIHOMO_PATH="${MIHOMO_PATH:-/etc/mihomo}"
# 内核路径的唯一来源：mihomo.service / app.py / CLI 都用这个路径
CORE_BIN="/usr/bin/mihomo-core"
GH_PROXY="${GH_PROXY:-}"
CURL_MAX_TIME="${CURL_MAX_TIME:-300}"

TMP_DIR="$(mktemp -d)"
GZ_FILE="${TMP_DIR}/mihomo.gz"
BIN_FILE="${TMP_DIR}/mihomo"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
    echo "❌ $*" >&2
    exit 1
}

# 架构检测
ARCH=$(uname -m)
if [[ "$ARCH" == "x86_64" ]]; then
    PLATFORM="linux-amd64-compatible"
elif [[ "$ARCH" == "aarch64" ]]; then
    PLATFORM="linux-arm64"
else
    fail "不支持的架构: $ARCH"
fi

# GitHub 地址候选：先直连，失败再走 .env 里的 GH_PROXY（为空就只直连）
github_candidates() {
    local url="$1"
    echo "$url"
    if [ -n "$GH_PROXY" ]; then
        echo "${GH_PROXY%/}/${url}"
    fi
}

# 依次尝试候选地址下载到指定文件，全部失败返回 1
fetch_first() {
    local output="$1"
    local url="$2"
    local candidate
    while read -r candidate; do
        [ -n "$candidate" ] || continue
        if curl -fL --max-time "$CURL_MAX_TIME" --retry 2 --retry-delay 2 -o "$output" "$candidate"; then
            return 0
        fi
        echo "⚠️  下载失败: $candidate" >&2
        rm -f "$output"
    done < <(github_candidates "$url")
    return 1
}

# ==========================================
# 获取最新版本号（拿不到就中止，绝不退回写死的旧版本）
# ==========================================
latest_tag() {
    local tag=""
    # 1) GitHub API（只直连，代理一般不转发 api.github.com）
    tag=$(curl -fsSL --max-time 20 "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
    # 2) release 自带的 version.txt（直连 / GH_PROXY）
    if [ -z "$tag" ]; then
        local candidate
        while read -r candidate; do
            [ -n "$candidate" ] || continue
            tag=$(curl -fsSL --max-time 20 "$candidate" 2>/dev/null | tr -d '[:space:]')
            [ -n "$tag" ] && break
        done < <(github_candidates "https://github.com/MetaCubeX/mihomo/releases/latest/download/version.txt")
    fi
    printf '%s' "$tag"
}

installed_version() {
    if [ -x "$CORE_BIN" ]; then
        "$CORE_BIN" -v 2>/dev/null | head -n 1 | awk '{print $3}'
    fi
}

MODE=$1
if [[ "$MODE" == "auto" ]]; then
    echo "🤖 自动安装模式..."
else
    echo "正在获取版本列表..."
fi

TAG=$(latest_tag)
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+ ]]; then
    fail "无法获取 mihomo 最新版本号（GitHub API 和 version.txt 都不可达），已中止，未做任何改动。"
fi
echo "最新版本: ${TAG}"

# 不降级：已安装版本比最新 tag 还新（比如手动装了预发布版）就不动它
INSTALLED=$(installed_version)
if [ -n "$INSTALLED" ]; then
    echo "当前版本: ${INSTALLED}"
    NEWEST=$(printf '%s\n%s\n' "$TAG" "$INSTALLED" | sort -V | tail -n 1)
    if [ "$INSTALLED" == "$TAG" ]; then
        echo "✅ 当前已是最新版本，无需更新。"
        exit 0
    fi
    if [ "$NEWEST" == "$INSTALLED" ]; then
        fail "已安装版本 ${INSTALLED} 比最新发布 ${TAG} 更新，拒绝降级。"
    fi
fi

if [[ "$MODE" != "auto" ]]; then
    read -p "是否安装此版本? (y/n): " choice
    if [[ "$choice" != "y" ]]; then
        echo "已取消。"
        exit 0
    fi
fi

# ==========================================
# 下载、校验、安装（任一步失败都非零退出）
# ==========================================
DOWNLOAD_URL="https://github.com/MetaCubeX/mihomo/releases/download/${TAG}/mihomo-${PLATFORM}-${TAG}.gz"

echo "⬇️  正在下载内核 ${TAG} (${PLATFORM})..."
fetch_first "$GZ_FILE" "$DOWNLOAD_URL" || fail "内核下载失败！请检查网络或 .env 里的 GH_PROXY 设置。"
[ -s "$GZ_FILE" ] || fail "下载到的文件为空。"

# MetaCubeX/mihomo 的 release 只附带 version.txt，没有 sha256 校验文件，
# 这里只能用 gzip 完整性校验 + 安装后执行 -v 来确认二进制可用。
echo "🔎 正在校验压缩包..."
gzip -t "$GZ_FILE" || fail "压缩包损坏（gzip 校验失败），已中止。"

echo "📦 正在解压并安装..."
gunzip -f "$GZ_FILE" || fail "解压失败。"
[ -s "$BIN_FILE" ] || fail "解压后没有得到内核文件。"
chmod +x "$BIN_FILE" || fail "无法设置可执行权限。"
"$BIN_FILE" -v >/dev/null 2>&1 || fail "下载的内核无法执行（架构不匹配或文件损坏），已中止。"

# 先放到同目录的临时名再 mv，替换是原子的，不会出现半截二进制
install -m 0755 "$BIN_FILE" "${CORE_BIN}.new" || fail "无法写入 ${CORE_BIN}.new"
mv -f "${CORE_BIN}.new" "$CORE_BIN" || fail "无法替换 ${CORE_BIN}"
echo "✅ 内核已安装到 ${CORE_BIN}: $("$CORE_BIN" -v 2>/dev/null | head -n 1)"

# ==========================================
# 智能重启：只有服务正在运行时才重启，初次安装时服务是停止的，跳过
# ==========================================
if systemctl is-active --quiet mihomo.service; then
    echo "🔄 检测到服务正在运行，正在重启以应用新内核..."
    systemctl restart mihomo || fail "服务重启失败，请查看 journalctl -u mihomo。"
    echo "✅ 服务重启完成。"
else
    echo "✅ 内核安装完成 (服务未启动，请在配置订阅后手动启动)。"
fi
