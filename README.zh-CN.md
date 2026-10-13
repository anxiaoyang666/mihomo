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
- `WEB_HOST`：面板监听地址，默认 `0.0.0.0`（和原来一样，已有的局域网安装不会突然只能本机访问）。设成 `127.0.0.1` 则只接受本机连接。
- `WEB_USER`：Web 登录用户名，默认 `admin`
- `WEB_SECRET`：Web 登录密码，不填写则随机生成。面板保存的是加盐哈希（Werkzeug 的 scrypt，或 pbkdf2）。`.env` 里已有的明文会在启动时自动换成哈希；哈希失败时仍按明文登录，不会把人锁在外面。
- `WEB_COOKIE_SECURE`：`true` 或 `false`，默认 `false`。见下面的 TLS 说明。
- `GH_PROXY`：GitHub 代理前缀，默认 `https://gh-proxy.com/`
- `GH_PROXY_BINARIES`：设为 `true` 才允许 mihomo 内核下载在直连失败后走 `GH_PROXY`。默认 `false`。资产摘要不走代理。
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

- `GH_PROXY`：GitHub 代理前缀，只在直连 GitHub 失败后作为回退使用（留空表示不用代理）。面板升级可以走它下载压缩包，但提交号和文件树必须先从 `api.github.com` 直连拿到，压缩包要和那次提交逐文件一致。
- `GH_PROXY_BINARIES`：设为 `true` 才允许用同一个前缀下载 mihomo 内核。默认 `false`。走代理的内核只有在大小和 `sha256` 都对上直连 `api.github.com` 拿到的发布资产时才会安装。没有 digest 时，代理下载会被拒绝；直连 GitHub 的下载仍会退回 gzip 完整性检查。
- `WEB_HOST`：监听地址，默认 `0.0.0.0`。修改后需要重启 `mihomo-manager`。
- `WEB_COOKIE_SECURE`：`true` 时给会话 Cookie 加上 Secure。默认 `false`。
- `MIHOMO_API_SECRET`：`external-controller` 的访问密钥，由 `install.sh` 生成并同时写入 `config.yaml` 和 `.env`（面板和 9090 端口的外部 Dashboard 都用它）
- `BACKUP_KEEP_COUNT`：`config.yaml` 备份保留数量，默认 `10`
- `ALLOW_INSECURE_TLS=true`：下载订阅 / Geo 数据时跳过 TLS 证书校验（默认关闭）
- `GATEWAY_AUTOFIX=false`：让每分钟一次的 `gateway_init.sh check` 定时任务不再自动修正 ip_forward / rp_filter / FORWARD / MASQUERADE（默认 `true`；手动执行脚本仍会应用）
- `RULE_SYNC_ENABLED`：`true` 时把面板里的强制直连 / 强制代理规则推送到其他 mosctl / mihomo 面板（默认 `false`）
- `RULE_SYNC_TOKEN`：规则同步的共享密钥，由 `install.sh` 生成，各节点必须一致
- `RULE_SYNC_PEERS`：其他面板地址，用 `|` 分隔（面板的同步设置页会写这个键）

TLS：面板是 Flask 自带的 HTTP 开发服务器，自己不终止 TLS。`SESSION_COOKIE_SECURE` 默认关闭，因为浏览器在明文 `http://` 上会丢掉带 Secure 的 Cookie，登录后马上又掉线。只有浏览器始终通过 HTTPS 访问面板时（例如你自己的反向代理）才把 `WEB_COOKIE_SECURE` 设为 `true`。面板进程和反代之间仍然是 HTTP。局域网明文安装请保持默认。

面板在把密码做成哈希时还会写 `WEB_PASSWORD_STRONG`（`true` 或 `false`），这样安全页在明文消失后仍能提示“太短或全是数字”。不要手改这个值。

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
