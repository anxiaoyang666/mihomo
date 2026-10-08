# Mihomo Toolbox

[English](README.md) | [中文](README.zh-CN.md)

Mihomo Toolbox 是一个用于网关式 Mihomo 服务的 Web 面板和辅助脚本集合。

## 一键安装

推荐在 Debian/Ubuntu LXC、虚拟机或服务器中使用 `root` 执行：

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

国内网络可以使用加速地址：

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

安装完成后，终端会打印 Web 面板地址、用户名和密码。默认 Web 端口是 `7838`，默认用户名是 `admin`。

自定义安装参数：

```bash
WEB_PORT=7838 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

可用变量：

- `MIHOMO_REPO_URL`：仓库地址，默认 `https://github.com/anxiaoyang666/mihomo.git`
- `MIHOMO_BRANCH`：安装分支，默认 `main`
- `WEB_PORT`：Web 面板端口，默认 `7838`
- `WEB_USER`：Web 登录用户名，默认 `admin`
- `WEB_SECRET`：Web 登录密码，不填写则随机生成
- `GH_PROXY`：GitHub 代理前缀，默认 `https://gh-proxy.com/`
- `MIHOMO_KEEP_ENV`：保留已有 `/etc/mihomo/.env`，默认 `1`

## 在线升级面板

新版本面板可以从本仓库在线升级。面板会从 `/etc/mihomo/.env` 读取以下可选配置：

```bash
MIHOMO_PANEL_REPO_URL="https://github.com/anxiaoyang666/mihomo.git"
MIHOMO_PANEL_BRANCH="main"
# 始终先直连 GitHub，GH_PROXY 只作为失败后的回退（留空表示不用代理）
GH_PROXY="https://gh-proxy.com/"
```

其他可选的 `.env` 配置：

- `GH_PROXY`：GitHub 代理前缀，只在直连 GitHub 失败后作为回退使用（留空表示不用代理）
- `MIHOMO_API_SECRET`：`external-controller` 的访问密钥，由 `install.sh` 生成并同时写入 `config.yaml` 和 `.env`（面板和 9090 端口的外部 Dashboard 都用它）
- `BACKUP_KEEP_COUNT`：`config.yaml` 备份保留数量，默认 `10`
- `ALLOW_INSECURE_TLS=true`：下载订阅 / Geo 数据时跳过 TLS 证书校验（默认关闭）
- `GATEWAY_AUTOFIX=false`：让每分钟一次的 `gateway_init.sh check` 定时任务不再自动修正 ip_forward / rp_filter / FORWARD / MASQUERADE（默认 `true`；手动执行脚本仍会应用）
- `RULE_SYNC_ENABLED`：`true` 时把面板里的强制直连 / 强制代理规则推送到其他 mosctl / mihomo 面板（默认 `false`）
- `RULE_SYNC_TOKEN`：规则同步的共享密钥，由 `install.sh` 生成，各节点必须一致
- `RULE_SYNC_PEERS`：其他面板地址，用 `|` 分隔（面板的同步设置页会写这个键）

面板升级会保留本地运行状态：

- `/etc/mihomo/.env`
- `/etc/mihomo/config.yaml`
- `/etc/mihomo/ui`

## 老版本面板一次性升级

如果旧安装还没有在线升级按钮，可以在安装 mihomo 的宿主机或容器里通过 SSH 执行：

```bash
bash -c 'set -e; TMP=$(mktemp -d); mkdir -p /etc/mihomo/backup; curl -fL -o "$TMP/mihomo.zip" "https://gh-proxy.com/https://github.com/anxiaoyang666/mihomo/archive/refs/heads/main.zip"; python3 - <<PY "$TMP/mihomo.zip" "$TMP"
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
SRC=$(find "$TMP" -maxdepth 3 -type d -name remote-root | head -n1); test -n "$SRC"; BACKUP="/etc/mihomo/backup/manager.before-upgrade.$(date +%Y%m%d%H%M%S)"; [ -d /etc/mihomo/manager ] && cp -a /etc/mihomo/manager "$BACKUP" || true; install -m 0755 "$SRC/usr/bin/mihomo" /usr/bin/mihomo; rm -rf /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; mkdir -p /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; cp -a "$SRC/etc/mihomo/manager/." /etc/mihomo/manager/; cp -a "$SRC/etc/mihomo/scripts/." /etc/mihomo/scripts/; cp -a "$SRC/etc/mihomo/templates/." /etc/mihomo/templates/; install -m 0644 "$SRC/etc/systemd/system/mihomo.service" /etc/systemd/system/mihomo.service; install -m 0644 "$SRC/etc/systemd/system/mihomo-manager.service" /etc/systemd/system/mihomo-manager.service; install -m 0644 "$SRC/etc/systemd/system/force-ip-forward.service" /etc/systemd/system/force-ip-forward.service; install -m 0644 "$SRC/etc/mihomo/config.example.yaml" /etc/mihomo/config.example.yaml; mkdir -p /etc/logrotate.d; install -m 0644 "$SRC/etc/logrotate.d/mihomo" /etc/logrotate.d/mihomo; systemctl daemon-reload; systemctl restart mihomo-manager; rm -rf "$TMP"; echo "Mihomo panel upgraded. Backup: $BACKUP"'
```

这条命令替换的文件和面板在线升级管理的一致：`/usr/bin/mihomo`、`manager/`、`scripts/`、`templates/`、`config.example.yaml`、三个 systemd 单元和 `/etc/logrotate.d/mihomo`；`.env`、`config.yaml` 和 `ui/` 不会被动。

## 目录结构

- `remote-root/`：安装或升级时复制到系统绝对路径的文件。
- `tests/`：面板升级和安装行为的契约测试。

## 不发布的本地文件

仓库会忽略运行中的密钥、订阅、provider 缓存、运行配置、解包清单和归档文件。
