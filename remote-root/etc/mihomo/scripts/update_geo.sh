#!/bin/bash
# update_geo.sh - Geo 数据库更新 (完全静默)

MIHOMO_DIR="/etc/mihomo"
GEO_DIR="${MIHOMO_DIR}"
ENV_FILE="${MIHOMO_DIR}/.env"
TMP_DIR="$(mktemp -d)"
GEOIP_TMP="${TMP_DIR}/geoip.dat"
GEOSITE_TMP="${TMP_DIR}/geosite.dat"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ -f "$ENV_FILE" ]; then source "$ENV_FILE"; fi

echo "⬇️  开始更新 Geo 数据库..."

GEOIP_URL="https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat"
GEOSITE_URL="https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat"

# GeoIP
wget --no-check-certificate -O "$GEOIP_TMP" "$GEOIP_URL" >/dev/null 2>&1
if [ $? -eq 0 ] && [ -s "$GEOIP_TMP" ]; then
    mv "$GEOIP_TMP" "${GEO_DIR}/geoip.dat"
    echo "✅ GeoIP 更新成功"
else
    echo "❌ GeoIP 更新失败"
fi

# GeoSite
wget --no-check-certificate -O "$GEOSITE_TMP" "$GEOSITE_URL" >/dev/null 2>&1
if [ $? -eq 0 ] && [ -s "$GEOSITE_TMP" ]; then
    mv "$GEOSITE_TMP" "${GEO_DIR}/geosite.dat"
    echo "✅ GeoSite 更新成功"
else
    echo "❌ GeoSite 更新失败"
fi

# 即使更新了也不重启，或者重启但不通知
systemctl restart mihomo
echo "🏁 Geo 更新任务结束 (静默模式)"
