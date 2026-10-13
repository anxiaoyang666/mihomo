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

# 架构检测（manager/auto_update.py 的 CORE_PLATFORMS / core_asset 用同一套平台名和下载地址，改这里要同步改那边）
ARCH=$(uname -m)
if [[ "$ARCH" == "x86_64" ]]; then
    PLATFORM="linux-amd64-compatible"
elif [[ "$ARCH" == "aarch64" ]]; then
    PLATFORM="linux-arm64"
else
    fail "不支持的架构: $ARCH"
fi

# 二进制走代理必须单独打开。GH_PROXY 仍可用于 version.txt 这类元数据。
proxy_binaries_enabled() {
    case "$(printf '%s' "${GH_PROXY_BINARIES:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# GitHub 地址候选：先直连。kind=meta（默认）失败后再走 GH_PROXY；
# kind=binary 只有 GH_PROXY_BINARIES=true 才把代理加进来，并且之后必须对上 GitHub 的 sha256。
github_candidates() {
    local url="$1"
    local kind="${2:-meta}"
    printf '%s\n' "$url"
    if [ -z "${GH_PROXY:-}" ]; then
        return 0
    fi
    if [ "$kind" = "binary" ] && ! proxy_binaries_enabled; then
        return 0
    fi
    printf '%s\n' "${GH_PROXY%/}/${url}"
}

# 依次尝试候选地址下载到指定文件，全部失败返回 1。成功时 FETCHED_URL 是实际命中的地址。
FETCHED_URL=""
fetch_first() {
    local output="$1"
    local url="$2"
    local kind="${3:-meta}"
    local candidate
    while read -r candidate; do
        [ -n "$candidate" ] || continue
        if curl -fL --max-time "$CURL_MAX_TIME" --retry 2 --retry-delay 2 -o "$output" "$candidate"; then
            FETCHED_URL="$candidate"
            return 0
        fi
        echo "⚠️  下载失败: $candidate" >&2
        rm -f "$output"
    done < <(github_candidates "$url" "$kind")
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
ASSET_NAME="mihomo-${PLATFORM}-${TAG}.gz"
DOWNLOAD_URL="https://github.com/MetaCubeX/mihomo/releases/download/${TAG}/${ASSET_NAME}"
META_JSON="${TMP_DIR}/release.json"
# 资产的 size/digest 只从 api.github.com 直连读取。代理可以改写摘要，所以这里不走 GH_PROXY。
if ! curl -fsSL --max-time 20 -H "Accept: application/vnd.github+json" -H "User-Agent: mihomo-toolbox" \
    -o "$META_JSON" "https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/${TAG}"; then
    echo "⚠️  无法直接访问 GitHub API 读取 ${TAG} 的资产摘要（不会改走代理）。" >&2
    rm -f "$META_JSON"
fi

echo "⬇️  正在下载内核 ${TAG} (${PLATFORM})..."
fetch_first "$GZ_FILE" "$DOWNLOAD_URL" binary || fail "内核下载失败！请检查网络。二进制走代理需要在 .env 里设置 GH_PROXY_BINARIES=true。"
[ -s "$GZ_FILE" ] || fail "下载到的文件为空。"

VIA_PROXY=0
case "$FETCHED_URL" in
    https://github.com/*) VIA_PROXY=0 ;;
    *) VIA_PROXY=1 ;;
esac
# 有 GitHub digest/size 就核对；直连且 API 没给 sha256 时，下面仍用 gzip -t 和 -v。
# 走代理却没有 sha256 则拒绝安装。校验实现在 verify_github_asset.py，自动更新走同一套规则。
VERIFY_PY="$(cd "$(dirname "$0")" && pwd)/verify_github_asset.py"
python3 "$VERIFY_PY" "$GZ_FILE" "${META_JSON:-/dev/null}" "$ASSET_NAME" "$VIA_PROXY" \
    || fail "内核校验未通过，已中止，未替换 ${CORE_BIN}。"

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
