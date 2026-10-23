# Mihomo Toolbox

[English](README.md) | [中文](README.zh-CN.md)

本仓库是 **mihomo 网关的 Web 面板和配套脚本**。维护文档以中文为主。当前部署形态是：装在 **Proxmox VE（PVE）的 LXC 容器**里，由容器内的 **systemd** 拉起，不用 Docker。

面板版本号写在 `remote-root/etc/mihomo/manager/app.py` 的 `PANEL_VERSION`（仓库里现在是 `0.1.62`）。架构和排障细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/MAINTENANCE.md](docs/MAINTENANCE.md)。

English readers: the install commands are in [One-Click Install](#one-click-install). The rest of this file is the Chinese operator guide.

## 简介

容器作为局域网网关运行 [mihomo](https://github.com/MetaCubeX/mihomo)（Clash Meta）。面板负责登录、订阅、配置、定时任务、规则同步、通知、设备流量和在线升级；内核负责代理。两者是两个 systemd 服务：

| 单元 | 进程 | 作用 |
| --- | --- | --- |
| `mihomo.service` | `/usr/bin/mihomo-core -d /etc/mihomo` | 代理内核。日志重定向到 `/var/log/mihomo.log` |
| `mihomo-manager.service` | `python3 /etc/mihomo/manager/app.py` | Flask 面板，默认监听 `0.0.0.0:7838` |
| `force-ip-forward.service` | `sysctl -w net.ipv4.ip_forward=1` | 开机打开 IPv4 转发（oneshot） |

命令行入口是 `/usr/bin/mihomo`（仓库里的 `remote-root/usr/bin/mihomo`）。交互菜单和 `mihomo start|stop|restart|log` 都走它。

默认对外端口来自安装脚本和示例配置，不是面板代码里写死的业务端口：

- Web 面板 `7838`（`.env` 的 `WEB_PORT`）
- 内核控制器 `9090`（`config.yaml` 的 `external-controller`，示例是 `0.0.0.0:9090`，外部 Dashboard 在 `http://<容器IP>:9090/ui`）
- 混合代理 `7890`、DNS `53`、TUN：只在使用仓库自带模板 / `config.example.yaml` 时才是这些值。托管订阅若下发了别的配置，以那份 `config.yaml` 为准

## 功能

Web 面板（`templates/index.html`）的页面对应这些能力：

- **概览**：内核是否在跑、连接数、内存、上传下载、策略组、今日流量（按北京时间换日）、最近 10 分钟速率。按钮可启动 / 停止 / 重启内核、立即更新订阅、初始化网关、升级面板，并打开内核 Dashboard。
- **设备**：每 5 秒从控制器 `/connections` 采样。可选爱快（iKuai）只读 API，用路由器上的终端名、MAC、速率和流量替换列表；没配爱快时用连接和邻居表。
- **订阅与任务**：配置托管（整份 `config.yaml`）或节点订阅（套 `templates/default.yaml`）；编辑 `config.yaml`；订阅 / Geo 定时任务；自动更新 Dashboard、内核、面板。
- **分流同步**：维护「强制直连」「强制代理」域名，写入规则文件并热刷新；可推到其他同样接了这套接口的 mihomo / mosctl 面板。
- **通知中心**：Webhook（JSON `title` / `content`）。站点名出现在标题里。Telegram 只在 `.env` 里开，页面上没有表单。
- **运行日志**：内核、订阅、Geo、自动更新、通知，只读文件尾部。
- **账户安全**：改用户名和密码，并检查口令强度、控制器是否对局域网开放、`secret` 是否够长。

保存 `config.yaml` 时，没改监听、TUN、DNS 模式这类键会走控制器热加载（`PUT /configs?force=true`），不重启进程。改了 `tun`、端口、`external-controller`、`secret`、`dns.listen` 等才会 `systemctl restart mihomo`。比较失败时保守重启。

## 技术栈

- **运行时**：Python 3 + 发行版自带的 Flask（`python3-flask`）。PyYAML（`python3-yaml` / `PyYAML`）用于订阅生成和配置比较；没装时改配置会一律重启。面板用 Flask 自带的开发服务器 `app.run()`，前面没有 gunicorn，进程自己也不终止 TLS。
- **前端**：单页 `index.html`，静态资源在 `manager/static/`（含随仓库打包的 Bootstrap 与 Bootstrap Icons）。
- **内核**：GitHub `MetaCubeX/mihomo` 的发布包，装到 `/usr/bin/mihomo-core`。支持 `x86_64`（资源名 `linux-amd64-compatible`）和 `aarch64`（`linux-arm64`）。
- **系统**：systemd、cron（`cron` 或 `crond`）、iptables、iproute2、logrotate。脚本是 bash。
- **仓库检查**：GitHub Actions 跑 pytest（Python 3.12）和 ShellCheck，任务在飞牛自托管 runner（`self-hosted`, `xiguo`）上执行，runner 需预装 ShellCheck。见 `.github/workflows/ci.yml`。

## One-Click Install

在 PVE LXC 里用 **root** 执行。`install.sh` 认 `apt-get`（Debian/Ubuntu）、`dnf`、`yum`，按这个顺序选包管理器。当前这套部署用的是 Debian/Ubuntu 容器。

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

国内网络可以使用加速地址：

```bash
bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

装完后终端会打印面板地址、用户名、密码，以及 `9090` 上 Dashboard 的密钥。默认 Web 端口是 `7838`，默认用户名是 `admin`。密码不填则随机生成。**先看安装输出里的明文密码**：面板一起动就会把 `.env` 里的明文 `WEB_SECRET` 换成加盐哈希，之后再读文件就不是原密码了。

自定义安装参数：

```bash
WEB_PORT=7838 WEB_USER=admin WEB_SECRET='your-password' bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"
```

只在安装当时生效的变量：

| 变量 | 默认 | 写入 `.env` 的键 |
| --- | --- | --- |
| `MIHOMO_REPO_URL` | `https://github.com/anxiaoyang666/mihomo.git` | `MIHOMO_PANEL_REPO_URL` |
| `MIHOMO_BRANCH` | `main` | `MIHOMO_PANEL_BRANCH` |
| `WEB_PORT` | `7838` | `WEB_PORT` |
| `WEB_HOST` | `0.0.0.0` | `WEB_HOST` |
| `WEB_USER` | `admin` | `WEB_USER` |
| `WEB_SECRET` | 随机 | `WEB_SECRET`（启动后面板会改成哈希） |
| `WEB_COOKIE_SECURE` | `false` | `WEB_COOKIE_SECURE` |
| `GH_PROXY` | `https://gh-proxy.com/` | `GH_PROXY` |
| `GH_PROXY_BINARIES` | `false` | `GH_PROXY_BINARIES` |
| `MIHOMO_KEEP_ENV` | `1` | 不写入。为 `1` 且已有 `/etc/mihomo/.env` 时整份保留，不覆盖 |

已经在仓库目录里执行 `bash install.sh` 时，脚本发现旁边有 `remote-root/` 就不再 clone。

安装脚本实际做的事：

1. 安装 `ca-certificates curl wget unzip gzip git python3 python3-flask python3-yaml iproute2 iptables nano cron`（dnf/yum 用对应包名，cron 包是 `cronie`）。
2. 把 `remote-root/` 拷到系统路径：`/usr/bin/mihomo`、`/etc/mihomo/manager`、`scripts`、`templates`、三个 systemd 单元、`/etc/logrotate.d/mihomo`。已有 `config.yaml` 不覆盖；没有则从 `config.example.yaml` 复制。
3. 写 `/etc/mihomo/.env`（权限 `0600`），生成 `WEB_SESSION_SECRET`、`MIHOMO_API_SECRET`、`RULE_SYNC_TOKEN`。
4. 把控制器 `secret` 和 `MIHOMO_API_SECRET` 对齐。`config.yaml` 里已有非空 `secret` 时以它为准。
5. 按 `CRON_SUB_*` 写入 root crontab 的订阅任务（标记 `# JOB_SUB`），并尝试 `systemctl enable --now cron` 或 `crond`。
6. 没有 `/usr/bin/mihomo-core` 时调用 `scripts/install_kernel.sh auto`。内核下失败**不会**让整个安装失败，面板仍然起来。
7. 打印账号，然后 `daemon-reload`，enable/restart `force-ip-forward` 和 `mihomo-manager`。内核和配置都在时再 enable/restart `mihomo`。

`install.sh` **不会**自动跑 `gateway_init.sh`。转发保活要在面板里点「初始化网关」，或在容器里执行 `mihomo` 菜单的第 8 项。`force-ip-forward.service` 只负责 `ip_forward=1`。

容器要能做这些事，否则对应功能起不来：`systemctl`、root 的 `crontab`、`iptables`、`sysctl`（`ip_forward` / `rp_filter`），以及 mihomo 的 TUN（自带模板里 `tun.enable: true`）。非特权 LXC 经常默认没有 TUN 和改防火墙的权限，需要在 PVE 上给这只容器放行；本仓库不包含宿主机的 PVE 配置。

## 环境变量

运行配置是 `/etc/mihomo/.env`，权限 `0600`。这是 shell 赋值，由 Python `shlex` 解析（`comments=False`，未加引号的值里的 `#` 不当注释）。写这个文件要用面板、`install.sh`，或 `scripts/envutil.sh` 的 `upsert_env`，不要用 bash 的 `printf %q`（它会写出面板解析不了的 `$'...'`）。

改 `WEB_HOST`、`WEB_PORT`、`WEB_COOKIE_SECURE` 后要 `systemctl restart mihomo-manager`。

### 面板与账号

| 键 | 谁写入 | 含义 |
| --- | --- | --- |
| `WEB_USER` | 安装 / 账户页 | 登录名。账户页限制 3–32 位字母、数字、`_`、`.`、`-` |
| `WEB_SECRET` | 安装，随后面板改成哈希 | 密码。明文、`scrypt:`、`pbkdf2:` 都能登录。哈希失败时保留明文，不会把人锁在外面 |
| `WEB_PASSWORD_STRONG` | 面板在哈希时写 `true`/`false` | 安全页用它判断「太短或全是数字」。强的标准是不少于 10 位且不全是数字。不要手改 |
| `WEB_SESSION_SECRET` | 安装生成；改密时轮换 | Flask session 签名。改密会使其他浏览器的登录失效 |
| `WEB_PORT` | 安装，默认 `7838` | 非法或超出 1–65535 时面板改用 `7838` |
| `WEB_HOST` | 安装，默认 `0.0.0.0` | 非法地址退回 `0.0.0.0`。`127.0.0.1` 只接受本机 |
| `WEB_COOKIE_SECURE` | 安装，默认 `false` | `true` 才给会话 Cookie 加 Secure。见下面 TLS |
| `SITE_NAME` | 通知页 | 通知标题里的站点名，最多 20 字。空则显示「mihomo 网关」 |

同一 IP 连续登录失败 5 次会锁 60 秒。登录态 30 天，Cookie 为 HttpOnly、`SameSite=Lax`。除登录、登出和 `/api/site-info`、`/api/rule-sync` 外，接口要登录；写操作还要带 `X-Requested-With: XMLHttpRequest`。

TLS：面板只说 HTTP。浏览器走明文 `http://` 时，Secure Cookie 会被丢掉，登录立刻掉线。只有浏览器始终经你自己的 HTTPS 反向代理访问时，才把 `WEB_COOKIE_SECURE` 设为 `true`。面板和反代之间仍然是 HTTP。局域网明文安装保持默认。

### 订阅、网关、备份

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `CONFIG_MODE` | 安装写成 `raw` | `raw` 配置托管：下载 `SUB_URL_RAW`，整份当 `config.yaml`。`airport` 节点订阅：用 `SUB_URL_AIRPORT`（多行或 `\|`）套 `templates/default.yaml`。键缺失时面板设置页按 `airport` 显示 |
| `SUB_URL_RAW` / `SUB_URL_AIRPORT` | 空 | 上面两种链接。机场多条在 `.env` 里把换行存成 `\n` |
| `SUB_USER_AGENT` | `clash.meta` | 只给 `update_subscription.sh` 的 wget。很多机场按 UA 决定返回 YAML 还是 base64 节点 |
| `LOCAL_CIDR` | 空 | 本机局域网网段，订阅更新时注入为直连，避免绕进指向自己的隧道。空则按本机地址自动识别。最多 8 个，只接受局域网地址 |
| `BACKUP_KEEP_COUNT` | 安装写成 `20`；键缺失或非法时脚本和面板回落到 `10` | `config_*.yaml` 和 `config.before-rule-sync.*.yaml` 保留份数 |
| `MIHOMO_PATH` | `/etc/mihomo` | 安装写入。脚本的兜底也是这个路径 |
| `SCRIPT_PATH` | `/etc/mihomo/scripts` | 同上 |
| `MIHOMO_API_SECRET` | 安装随机生成 | 控制器 Bearer。和 `config.yaml` 的 `secret` 保持一致。面板、订阅回填、Geo 刷新都用它 |
| `MIHOMO_CONTROLLER` | 空 | 可选。面板访问控制器的地址，缺省读 `external-controller`，再缺省 `127.0.0.1:9090`。`0.0.0.0` 会改成 `127.0.0.1` |
| `ALLOW_INSECURE_TLS` | 不写即关闭 | 只有精确等于 `true` 时，订阅和 Geo 的 wget 才加 `--no-check-certificate` |
| `GATEWAY_AUTOFIX` | 不写即 `true` | `false` 时，每分钟的 `gateway_init.sh check` 直接退出。手动执行脚本仍会改 ip_forward / rp_filter / FORWARD / MASQUERADE |

### 定时任务与自动更新

订阅和 Geo 的 cron 由面板「订阅与任务」写入 root crontab。时间按**容器时区**执行；页面上按浏览器本地时间换算显示。容器若是 UTC，要按页面上的时区提示理解。

| 键 | 含义 |
| --- | --- |
| `CRON_SUB_ENABLED` / `CRON_SUB_SCHED` / `CRON_SUB_MODE` / `CRON_SUB_TIME` | 订阅。安装默认开启，`0 5 * * *`（每天 05:00）。标记 `# JOB_SUB`，日志 `/var/log/mihomo-subscription.log` |
| `CRON_GEO_ENABLED` / `CRON_GEO_SCHED` / `CRON_GEO_MODE` / `CRON_GEO_TIME` | Geo。安装时不写，要在面板里打开。页面默认展示 `0 4 * * *`。标记 `# JOB_GEO`，日志 `/var/log/mihomo-geo.log` |

模式 `daily` / `every6h` / `every12h` / `advanced`。高级模式只接受 5 段、字符集为数字和 `* / , -` 空格的表达式，否则退回默认。

自动更新由 `manager/auto_update.py` 执行。键缺失时面板按下面的默认值，并在启动时把 cron 补上（标记 `# MIHOMO_AUTO_UPDATE`）：

| 键 | 默认 | 范围 |
| --- | --- | --- |
| `AUTO_UPDATE_ENABLED` | 当成开启，除非写成 `false` | `true` / `false` |
| `AUTO_UPDATE_TIME` | `04:00` | `HH:MM` |
| `AUTO_UPDATE_CORE_MIN_AGE_DAYS` | `3` | 0–90，发布满这么多天才装稳定版 |
| `AUTO_UPDATE_PANEL_MIN_AGE_DAYS` | `0` | 0–90 |
| `AUTO_UPDATE_UI_INTERVAL_DAYS` | `7` | 1–365，Dashboard 的更新间隔 |

### 下载源、同步、通知、爱快

| 键 | 含义 |
| --- | --- |
| `MIHOMO_PANEL_REPO_URL` | 面板升级用的 GitHub 仓库，默认 `https://github.com/anxiaoyang666/mihomo.git`。只接受 `https://github.com/<owner>/<name>` |
| `MIHOMO_PANEL_BRANCH` | 分支，默认 `main` |
| `GH_PROXY` | GitHub 代理前缀，只在直连失败后作回退。留空表示不用。只接受 `http://` 或 `https://`。安装默认 `https://gh-proxy.com/` |
| `GH_PROXY_BINARIES` | `true` / `1` / `yes` / `on` 才允许内核压缩包走 `GH_PROXY`。默认 `false`。资产的 size / sha256 只从直连的 `api.github.com` 读取 |
| `RULE_SYNC_ENABLED` | `true` 时保存强制直连 / 强制代理规则后推送到对端。默认 `false` |
| `RULE_SYNC_TOKEN` | 同步共享密钥。对端请求头 `X-Mosdns-Sync-Token`（或 JSON 里的 `token`）必须一致。安装时随机生成 |
| `RULE_SYNC_PEERS` | 对端面板 URL，用 `\|` 分隔。同步设置页会写这个键 |
| `NOTIFY_API` / `NOTIFY_API_URL` | Webhook 开关和地址。POST JSON：`{"title","content"}` |
| `NOTIFY_TG` / `TG_BOT_TOKEN` / `TG_CHAT_ID` | Telegram。`notify.sh` 会发，**通知页不读写这三项**，只能改 `.env` |
| `IKUAI_URL` / `IKUAI_TOKEN` | 爱快只读 API。地址必须是 http(s) 的内网 / 回环 IP 或主机名，不能带路径和公网 IP。设备页保存 |

## 目录结构

仓库里要部署的文件都在 `remote-root/`，安装时按绝对路径拷走。

```text
install.sh                 一次性安装（root）
remote-root/
  usr/bin/mihomo           安装为 /usr/bin/mihomo，命令行菜单
  etc/systemd/system/      mihomo.service、mihomo-manager.service、force-ip-forward.service
  etc/logrotate.d/mihomo   五份日志，copytruncate，保留 7 天
  etc/mihomo/
    config.example.yaml    示例配置；没有 config.yaml 时安装脚本复制成它
    templates/default.yaml 节点订阅用的模板（和示例内容相同）
    scripts/               见下表
    manager/app.py         面板。PANEL_VERSION、路由、升级都在这里
    manager/auto_update.py 定时更新 Dashboard / 内核 / 面板
    manager/notify_format.py  通知标题、正文、失败去重
    manager/panel_healthcheck.py  升级重启后探测 /login，失败则回滚
    manager/templates/     login.html、index.html
    manager/static/        面板静态资源
tests/                     契约测试，不随安装拷走
docs/                      维护文档。docs/superpowers/plans/ 是历史实施计划，不是现行操作手册
```

`scripts/` 里每个脚本的入口：

| 脚本 | 谁调用 |
| --- | --- |
| `envutil.sh` | CLI `source`。`upsert_env` / `get_env`。安装脚本里有一份同逻辑的 `env_upsert` / `env_get` |
| `install_kernel.sh` | `install.sh`、CLI 菜单 1、面板自动更新走另一套 Python，但平台名和校验规则与它一致 |
| `update_subscription.sh` | CLI、面板「更新订阅」、`# JOB_SUB` |
| `update_geo.sh` | CLI 菜单 6、面板、`# JOB_GEO` |
| `gateway_init.sh` | CLI 菜单 8、面板「初始化网关」。`check` 参数供它自己写入的每分钟 cron |
| `notify.sh` | CLI 菜单 7、订阅 / Geo / 面板 `notify_event` |
| `uninstall.sh` | CLI 菜单 10。`uninstall.sh [-y] [--purge\|--keep-data]` |
| `verify_github_asset.py` | `install_kernel.sh` 和面板校验 GitHub 发布资产，自己不访问网络 |

运行中、不进 git 的路径（见 `.gitignore`）：`/etc/mihomo/.env`、`config.yaml`、`providers/`、`backup/`、`cache.db`（内核工作目录 `-d /etc/mihomo` 下的缓存）。另外还有面板写的 `rules/`、`ui/`、`devices.json`、`traffic_daily.json`、`.last_subscription`、`auto_update_state.json`、`notify_state.json`，以及 Geo 文件 `geoip.dat`、`geosite.dat`、`geoip.metadb`。

## 升级与回滚

在线升级和自动更新的细节、手动回滚命令见 [docs/MAINTENANCE.md](docs/MAINTENANCE.md#升级与回滚)。这里只列边界。

面板升级（概览里的升级，或自动更新的最后一步）替换的是 `panel_managed_targets()` 那一批：

- `/etc/mihomo/manager/`、`/etc/mihomo/scripts/`、`/etc/mihomo/templates/`
- `/usr/bin/mihomo`
- `/etc/mihomo/config.example.yaml`
- 三个 systemd 单元和 `/etc/logrotate.d/mihomo`

**不覆盖** `PANEL_UPGRADE_EXCLUDES`：`/etc/mihomo/.env`、`/etc/mihomo/config.yaml`、`/etc/mihomo/ui`。升级前把旧文件拷到 `/etc/mihomo/backup/mihomo-panel.<时间戳>/`，只留最近 3 份。替换完成后用 `systemd-run` 起 `panel_healthcheck.py`：等旧进程退出，对 `/login` 连续两次成功（HTTP 200 且页面里有密码框）才保留新版本，否则按备份清单还原并重新拉起 `mihomo-manager`。健康检查日志在 `/var/log/mihomo-panel-upgrade-health.log`。

提交必须先从直连的 `api.github.com` 钉死；压缩包可以在直连失败后走 `GH_PROXY`，但内容要和该提交的 git 树、`app.py` 的 sha256 一致。远端版本不高于当前 `PANEL_VERSION` 时不安装。

内核自动更新只装形如 `vX.Y.Z` 的稳定版，不降级，发布天数不够就跳过。替换前用新二进制 `-v` 和 `mihomo-core -t -d /etc/mihomo -f config.yaml`。替换后 30 秒内检查服务、控制器版本、DNS 和入站端口，失败则从 `/etc/mihomo/backup/mihomo-core.<旧版本>.<时间戳>` 换回，最多留 3 份内核备份。手动「更新内核」走 `install_kernel.sh`，它不看「发布满 N 天」，但同样拒绝降级。

### 老版本面板一次性升级

还没有在线升级按钮的旧安装，在**这只 LXC 里**用 root 执行。它替换的文件和在线升级同一批；`.env`、`config.yaml`、`ui/` 不动。

```bash
bash -c 'set -e; TMP=$(mktemp -d); mkdir -p /etc/mihomo/backup; curl -fL -o "$TMP/mihomo.zip" "https://gh-proxy.com/https://github.com/anxiaoyang666/mihomo/archive/refs/heads/main.zip"; python3 - <<PY "$TMP/mihomo.zip" "$TMP"
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
SRC=$(find "$TMP" -maxdepth 3 -type d -name remote-root | head -n1); test -n "$SRC"; BACKUP="/etc/mihomo/backup/manager.before-upgrade.$(date +%Y%m%d%H%M%S)"; [ -d /etc/mihomo/manager ] && cp -a /etc/mihomo/manager "$BACKUP" || true; install -m 0755 "$SRC/usr/bin/mihomo" /usr/bin/mihomo; rm -rf /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; mkdir -p /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; cp -a "$SRC/etc/mihomo/manager/." /etc/mihomo/manager/; cp -a "$SRC/etc/mihomo/scripts/." /etc/mihomo/scripts/; cp -a "$SRC/etc/mihomo/templates/." /etc/mihomo/templates/; install -m 0644 "$SRC/etc/systemd/system/mihomo.service" /etc/systemd/system/mihomo.service; install -m 0644 "$SRC/etc/systemd/system/mihomo-manager.service" /etc/systemd/system/mihomo-manager.service; install -m 0644 "$SRC/etc/systemd/system/force-ip-forward.service" /etc/systemd/system/force-ip-forward.service; install -m 0644 "$SRC/etc/mihomo/config.example.yaml" /etc/mihomo/config.example.yaml; mkdir -p /etc/logrotate.d; install -m 0644 "$SRC/etc/logrotate.d/mihomo" /etc/logrotate.d/mihomo; systemctl daemon-reload; systemctl restart mihomo-manager; rm -rf "$TMP"; echo "Mihomo panel upgraded. Backup: $BACKUP"'
```

这条一次性命令**没有**在线升级那套提交校验和 `/login` 健康检查。能用面板升级时优先用面板。

## 常见问题

- **面板地址打不开**：`systemctl status mihomo-manager`。确认 `.env` 的 `WEB_HOST` / `WEB_PORT`，以及 LXC 防火墙是否放行该端口。进程日志在 journal 里，不在 `/var/log/mihomo.log`。
- **登录后马上掉线**：多半是明文 HTTP 上开了 `WEB_COOKIE_SECURE=true`。改回 `false` 再重启 `mihomo-manager`。
- **忘了密码**：`.env` 里若已是 `scrypt:` / `pbkdf2:` 哈希，不能从文件反查。用 `upsert_env` 写回一个新的明文 `WEB_SECRET`，重启面板，启动时会再哈希。同时改 `WEB_USER` 也要写成合法用户名。
- **内核没起来**：`journalctl -u mihomo -n 50` 和 `/var/log/mihomo.log`。没有 `/usr/bin/mihomo-core` 时执行 `bash /etc/mihomo/scripts/install_kernel.sh auto`。配置错误时 `mihomo-core -t -d /etc/mihomo -f /etc/mihomo/config.yaml`。
- **订阅更新后面板连不上控制器**：托管配置可能把 `secret` 下空了。`update_subscription.sh` 会用 `MIHOMO_API_SECRET` 回填；两边不一致时以 `.env` 为准改 `config.yaml` 的 `secret`，再重启内核。
- **局域网设备访问自己网段绕进代理**：检查 `LOCAL_CIDR`，再执行一次订阅更新。`GATEWAY_AUTOFIX=false` 时，每分钟保活不会再改 iptables。
- **自动更新说在跑但没结果**：看 `/var/log/mihomo-auto-update.log` 和 `/etc/mihomo/auto_update_state.json`。锁文件是 `/run/mihomo-auto-update.lock`。

卸载：`mihomo` 菜单第 10 项，或 `bash /etc/mihomo/scripts/uninstall.sh`。默认询问是否删除 `/etc/mihomo`；`--keep-data` 保留，`--purge` 删除。它会停掉并删除三个单元、相关 crontab、`/etc/sysctl.d/99-mihomo-gateway.conf`、`/etc/logrotate.d/mihomo` 和两个二进制。

## 本地检查

功能代码在 `remote-root/`，测试不需要把服务装进本机：

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/
shellcheck --severity=warning install.sh remote-root/etc/mihomo/scripts/*.sh
```

依赖下限写在 `requirements-dev.txt`（Flask ≥ 2.0.1，PyYAML ≥ 5.4.1，pytest ≥ 7）。部分前端契约测试会调用 `node`，没有 node 时跳过；CI 使用 Node 20。ShellCheck 的 `SC1090` / `SC1091` 在 `.shellcheckrc` 里关闭，因为脚本 source 的是运行时 `.env`。

## 延伸阅读

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：进程、数据流、升级校验、规则同步
- [docs/MAINTENANCE.md](docs/MAINTENANCE.md)：systemd、cron、日志、升级回滚和排障命令
- [README.zh-CN.md](README.zh-CN.md)：中文快速安装，内容是本文件的精简版
