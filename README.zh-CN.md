# Mihomo Toolbox

[English](README.md) | [中文](README.zh-CN.md)

Mihomo Toolbox 是跑在 **Proxmox VE LXC** 里的 mihomo 网关面板和辅助脚本，由容器内的 **systemd** 管理（`mihomo`、`mihomo-manager`、`force-ip-forward`）。完整的功能、环境变量、目录和排障说明在 [README.md](README.md)，架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，日常操作见 [docs/MAINTENANCE.md](docs/MAINTENANCE.md)。

## 一键安装

在 LXC 里用 `root` 执行。安装脚本优先用 `apt-get`（Debian/Ubuntu），也接受 `dnf` / `yum`。

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

国内网络可以使用加速地址：

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

安装完成后，终端会打印 Web 面板地址、用户名和密码。默认 Web 端口是 `7838`，默认用户名是 `admin`。请当场记下密码：面板启动后会把明文 `WEB_SECRET` 换成加盐哈希。

自定义安装参数：

```bash
WEB_PORT=7838 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

常用安装变量：`MIHOMO_REPO_URL`、`MIHOMO_BRANCH`、`WEB_PORT`、`WEB_HOST`（默认 `0.0.0.0`）、`WEB_USER`、`WEB_SECRET`、`WEB_COOKIE_SECURE`（默认 `false`）、`GH_PROXY`（默认 `https://gh-proxy.com/`）、`GH_PROXY_BINARIES`（默认 `false`）、`MIHOMO_KEEP_ENV`（默认 `1`，保留已有 `/etc/mihomo/.env`）。

装完后的服务：

```bash
systemctl status mihomo-manager mihomo force-ip-forward
```

面板是 `python3 /etc/mihomo/manager/app.py`。内核是 `/usr/bin/mihomo-core -d /etc/mihomo`，标准输出进 `/var/log/mihomo.log`。命令行菜单是 `mihomo`。`install.sh` 不会自动执行 `gateway_init.sh`；需要网关转发保活时，在面板里初始化网关，或运行 `mihomo` 菜单第 8 项。

## 环境变量

运行配置在 `/etc/mihomo/.env`（`0600`）。改监听地址或 Cookie 后执行 `systemctl restart mihomo-manager`。完整表格见 [README.md](README.md#环境变量)。

- `GH_PROXY`：GitHub 代理前缀，只在直连失败后作回退（留空表示不用）。面板升级可以走它下压缩包，但提交号和文件树必须先从 `api.github.com` 直连拿到。
- `GH_PROXY_BINARIES`：设为 `true` 才允许用同一个前缀下载 mihomo 内核。默认 `false`。走代理的内核必须对上直连 API 拿到的 size 和 sha256。
- `WEB_HOST`：监听地址，默认 `0.0.0.0`。
- `WEB_COOKIE_SECURE`：`true` 时给会话 Cookie 加 Secure。面板自己不终止 TLS。明文 HTTP 上不要开，否则浏览器不保存登录态。
- `MIHOMO_API_SECRET`：`external-controller` 的密钥，由 `install.sh` 生成并写入 `config.yaml` 和 `.env`。面板和 `9090` 上的外部 Dashboard 都用它。
- `BACKUP_KEEP_COUNT`：`config.yaml` 备份保留份数。新安装写成 `20`；键不存在或非法时，面板和订阅脚本回落到 `10`。
- `ALLOW_INSECURE_TLS=true`：下载订阅 / Geo 时跳过 TLS 证书校验。不写则校验。
- `GATEWAY_AUTOFIX=false`：每分钟一次的 `gateway_init.sh check` 不再改 ip_forward / rp_filter / FORWARD / MASQUERADE。手动执行脚本仍会应用。默认相当于 `true`。
- `RULE_SYNC_ENABLED`：`true` 时把强制直连 / 强制代理规则推到其他 mihomo / mosctl 面板。默认 `false`。
- `RULE_SYNC_TOKEN`：规则同步共享密钥，各节点必须一致。
- `RULE_SYNC_PEERS`：对端面板地址，用 `|` 分隔。

面板哈希密码时还会写 `WEB_PASSWORD_STRONG`（`true` 或 `false`），供安全页提示弱口令。不要手改。

自动更新相关键（缺失时按默认值，面板启动会补 cron）：`AUTO_UPDATE_ENABLED`、`AUTO_UPDATE_TIME`（默认 `04:00`）、`AUTO_UPDATE_CORE_MIN_AGE_DAYS`（默认 `3`）、`AUTO_UPDATE_PANEL_MIN_AGE_DAYS`（默认 `0`）、`AUTO_UPDATE_UI_INTERVAL_DAYS`（默认 `7`）。

## 升级与回滚

新版本可在面板里在线升级，或由每天的 `auto_update.py` 升级。升级保留 `/etc/mihomo/.env`、`/etc/mihomo/config.yaml`、`/etc/mihomo/ui`。失败时健康检查会从 `/etc/mihomo/backup/mihomo-panel.<时间戳>/` 回滚。步骤见 [docs/MAINTENANCE.md](docs/MAINTENANCE.md#升级与回滚)。

还没有在线升级按钮的旧安装，在容器里执行：

```bash
bash -c 'set -e; TMP=$(mktemp -d); mkdir -p /etc/mihomo/backup; curl -fL -o "$TMP/mihomo.zip" "https://gh-proxy.com/https://github.com/anxiaoyang666/mihomo/archive/refs/heads/main.zip"; python3 - <<PY "$TMP/mihomo.zip" "$TMP"
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
SRC=$(find "$TMP" -maxdepth 3 -type d -name remote-root | head -n1); test -n "$SRC"; BACKUP="/etc/mihomo/backup/manager.before-upgrade.$(date +%Y%m%d%H%M%S)"; [ -d /etc/mihomo/manager ] && cp -a /etc/mihomo/manager "$BACKUP" || true; install -m 0755 "$SRC/usr/bin/mihomo" /usr/bin/mihomo; rm -rf /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; mkdir -p /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; cp -a "$SRC/etc/mihomo/manager/." /etc/mihomo/manager/; cp -a "$SRC/etc/mihomo/scripts/." /etc/mihomo/scripts/; cp -a "$SRC/etc/mihomo/templates/." /etc/mihomo/templates/; install -m 0644 "$SRC/etc/systemd/system/mihomo.service" /etc/systemd/system/mihomo.service; install -m 0644 "$SRC/etc/systemd/system/mihomo-manager.service" /etc/systemd/system/mihomo-manager.service; install -m 0644 "$SRC/etc/systemd/system/force-ip-forward.service" /etc/systemd/system/force-ip-forward.service; install -m 0644 "$SRC/etc/mihomo/config.example.yaml" /etc/mihomo/config.example.yaml; mkdir -p /etc/logrotate.d; install -m 0644 "$SRC/etc/logrotate.d/mihomo" /etc/logrotate.d/mihomo; systemctl daemon-reload; systemctl restart mihomo-manager; rm -rf "$TMP"; echo "Mihomo panel upgraded. Backup: $BACKUP"'
```

这条命令替换 `/usr/bin/mihomo`、`manager/`、`scripts/`、`templates/`、`config.example.yaml`、三个 systemd 单元和 `/etc/logrotate.d/mihomo`。它没有在线升级的提交校验和 `/login` 健康检查。

## 目录与卸载

- `remote-root/`：安装或升级时复制到系统绝对路径的文件。
- `tests/`：面板升级和安装行为的契约测试。
- 脚本：`envutil.sh`、`install_kernel.sh`、`update_subscription.sh`、`update_geo.sh`、`gateway_init.sh`、`notify.sh`、`uninstall.sh`、`verify_github_asset.py`。

卸载：`bash /etc/mihomo/scripts/uninstall.sh`。`--keep-data` 保留 `/etc/mihomo`，`--purge` 删除。仓库忽略运行中的密钥、订阅、provider 缓存、运行配置、解包清单和归档文件。
