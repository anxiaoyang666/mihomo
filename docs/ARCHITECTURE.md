# 架构

本文描述仓库里真实存在的进程和数据流，方便接手后改代码时知道该动哪一层。部署形态是 Proxmox VE 的 LXC，容器内用 systemd。操作命令见 [MAINTENANCE.md](MAINTENANCE.md)，安装和配置表见 [README.md](../README.md)。

## 两进程加一个开机 sysctl

```text
浏览器 :7838
    │  Cookie 会话（Flask）
    ▼
mihomo-manager.service
    python3 /etc/mihomo/manager/app.py
    │  读 /etc/mihomo/.env、config.yaml
    │  systemctl、crontab、bash scripts/*
    │  HTTP Bearer → 127.0.0.1:9090（external-controller）
    ▼
mihomo.service
    /usr/bin/mihomo-core -d /etc/mihomo
    │  TUN / mixed-port / DNS，按 config.yaml
    ▼
局域网客户端，或上游爱快把流量转到本机
```

`force-ip-forward.service` 是 `Type=oneshot`、`RemainAfterExit=yes`，只执行 `/sbin/sysctl -w net.ipv4.ip_forward=1`。NAT、FORWARD 策略和 `rp_filter` 不在这个单元里，而在 `scripts/gateway_init.sh`。

三个单元都是 `WantedBy=multi-user.target`，`After=network.target`。内核和面板是 `Type=simple`、`User=root`、`Restart=always`、`RestartSec=3`。没有 `NoNewPrivileges` 或 `PrivateTmp`。内核的 `ExecStart` 是 bash 重定向，所以 logrotate 必须 `copytruncate`（见 `/etc/logrotate.d/mihomo`）；面板的日志走 journal，因为 `ExecStart` 直接是 python3。

`app.py` 只在 `__main__` 里启动设备采样和自动更新收尾。`auto_update.py` 用 `import app` 调函数，不会再跑 Flask，也不会再起采样线程。

## 代码放哪

| 路径 | 职责 |
| --- | --- |
| `manager/app.py` | 路由、`.env` 读写、配置热加载、规则同步、面板升级、爱快、设备与流量采样 |
| `manager/auto_update.py` | cron / 手动触发的 Dashboard、内核、面板更新和内核健康检查 |
| `manager/notify_format.py` | 通知标题、正文、按 key 去重。标准库，无 Flask |
| `manager/panel_healthcheck.py` | 升级后的独立进程。禁止 import 新的 `app.py`，否则坏升级会让回滚脚本自己起不来 |
| `manager/templates/index.html` | 全部已登录页面，一个 HTML 里的 Bootstrap pills |
| `scripts/*.sh` | 安装后给 cron、CLI 和面板 `subprocess` 调用的副作用 |
| `usr/bin/mihomo` | 交互菜单。服务控制是 `systemctl`，不自己实现第二套启停 |
| `install.sh` | 只负责第一次装进系统。在线升级不走它，走 `upgrade_panel()` |

面板版本只有一处：`app.py` 顶部的 `PANEL_VERSION`。升级逻辑用正则 `(?m)^PANEL_VERSION\s*=\s*["']...["']` 读远端文件，换行或缩进写坏会让远端被当成没有版本、从而拒绝升级。

## 配置的两条来源

`/etc/mihomo/config.yaml` 是内核唯一的配置文件。它从哪来取决于 `.env` 的 `CONFIG_MODE`：

- `raw`：`update_subscription.sh` 用 wget 下载 `SUB_URL_RAW`，整份替换。适合 Sub-Store 或自己托管的完整 mihomo 配置。
- `airport`：读 `templates/default.yaml`，把 `SUB_URL_AIRPORT` 里的每条链接写成 `proxy-providers.Airport_01` 起的 http provider，再 `yaml.dump`。模板里的策略组和规则集保留。

两条路径之后都会：

1. 按 `LOCAL_CIDR`（空则用和面板相同的规则识别本机网段）注入直连，避免访问本机局域网时绕进「去其他站点」的隧道。最多 8 段，只接受 `10/8`、`172.16/12`、`192.168/16`、`100.64/10` 和 IPv6 `fc00::/7`。
2. 用 `MIHOMO_API_SECRET`（没有则用旧 `config.yaml` 的 `secret`）回填 `secret`，避免托管配置把控制器密钥清空。
3. 若面板能 import，调用 `reapply_sync_blocks()`，把强制直连 / 强制代理规则重新写进新配置。
4. 和旧配置做比较：不需要重启的差异走热加载，否则重启。应用失败或 3 秒后服务不是 active，就从本次备份拷回 `config.yaml` 再重启。

面板里直接改文本框走 `POST /api/config`：先 `mihomo-core -t -d /etc/mihomo -f <临时文件>`，通过后才替换，并同样在热加载和重启之间选择。没装 `mihomo-core` 时跳过校验直接保存。

需要重启的顶层键是 `tun`、`port`、`socks-port`、`mixed-port`、`redir-port`、`tproxy-port`、`allow-lan`、`bind-address`、`ipv6`、`listeners`、`external-controller`、`external-controller-tls`、`external-ui`、`secret`、`interface-name`、`routing-mark`、`profile`。DNS 键是 `listen`、`enable`、`enhanced-mode`、`fake-ip-range`。其余（节点、策略组、规则、`log-level`、`dns.nameserver`、`fake-ip-filter` 等）热加载。PyYAML 缺失或解析失败时保守重启。

热加载是对控制器 `PUT /configs?force=true`，随后再读 `/version` 确认还活着。

## 控制器

示例配置把 `external-controller` 放在 `0.0.0.0:9090`，并带 `external-ui: ui`。浏览器里的 Zashboard 是直连容器的 9090，不经过 Flask。所以 secret 不能空：空 secret 等于局域网里谁都能改配置、切节点。安装脚本生成随机 secret 写入配置和 `MIHOMO_API_SECRET`。

面板访问时把 `0.0.0.0` 和 `[::]` 换成 `127.0.0.1`，避免自己绕出去。可用 `MIHOMO_CONTROLLER` 覆盖。

外部 Dashboard 的文件在 `/etc/mihomo/ui`（或 `external-ui` 指向的目录）。自动更新调用控制器 `POST /upgrade/ui`，不重启内核。失败且原来有目录时，把更新前拷走的目录移回来。这个目录在面板升级的排除列表里，面板升级不会动它。

## 规则同步

可同步的只有两个 id：`force-cn`（DIRECT，强制直连）和 `force-nocn`（PROXY，走代理策略组）。域名存在：

- `/etc/mihomo/rules/mosctl-force-cn.yaml`
- `/etc/mihomo/rules/mosctl-force-nocn.yaml`

`config.yaml` 里用 file 类型的 rule-provider（名字 `mosctl_force_cn` / `mosctl_force_nocn`）引用它们，并在规则顶部、`fake-ip-filter`、`rule-providers` 三处打上 `MOSCTL_MIHOMO_*_BEGIN/END` 标记。改域名时写文件，再 `PUT /providers/rules/<name>`。标记块还不存在、热刷新失败时才重启内核。

`RULE_SYNC_ENABLED=true` 且配了 `RULE_SYNC_PEERS` 时，保存后在后台线程里推给对端。对端接口是 `POST /api/rule-sync`，**不走登录**，只认 `X-Mosdns-Sync-Token` 或 JSON 的 `token`，与 `RULE_SYNC_TOKEN` 做恒定时间比较。本机地址会从对端列表里去掉。推送进行中对端返回忙时，发起方会重试。

## 定时任务

全部写在 **root crontab**，不是 systemd timer。`update_cron()` 按行尾标记整行替换。

| 标记 | 命令 | 谁写入 |
| --- | --- | --- |
| `# JOB_SUB` | `bash /etc/mihomo/scripts/update_subscription.sh >> /var/log/mihomo-subscription.log 2>&1` | `install.sh`（默认每天 05:00）和面板 |
| `# JOB_GEO` | `bash .../update_geo.sh >> /var/log/mihomo-geo.log 2>&1` | 只在面板里打开后写入 |
| `# MIHOMO_AUTO_UPDATE` | `python3 /etc/mihomo/manager/auto_update.py >/dev/null 2>>/var/log/mihomo-auto-update.log` | 面板启动时按 `AUTO_UPDATE_*` 补齐。默认每天 04:00，且默认视为开启 |
| `gateway_init.sh check` | `/bin/bash .../gateway_init.sh check` | 只有手动跑过不带 `check` 的 `gateway_init.sh` 之后才有，每分钟一次 |

卸载和 CLI 的卸载兜底会按这些标记，以及旧的 `# MIHOMO_AUTOMATION`、`/etc/mihomo/scripts/`、`auto_update.py` 把行删掉。

`auto_update.py` 一次运行的顺序固定为 **Dashboard → 内核 → 面板**。内核若回滚，同一次运行跳过面板升级。面板升级会重启 `mihomo-manager`，所以手动触发优先用 `systemd-run --collect` 放进临时单元，避免升级把自己所在的 cgroup 杀掉。cron 本身已经在服务外面。

内核健康检查窗口是 30 秒、间隔 2 秒：`systemctl is-active mihomo`、控制器 `/version` 等于新版本、配置里开了 DNS 则向监听地址查 `www.google.com`、配置里声明的入站端口都在监听。任一失败就换回备份的旧二进制再查一遍。

面板升级在 `auto_update_state.json` 里先记 `started`。下一次面板进程启动时，`finalize_panel_auto_update()` 若发现正在跑的 `PANEL_VERSION` 已经不低于目标，把记录改成 `updated`。若这次启动距离 `started` 已超过 600 秒而版本仍旧，记成 `failed`。健康检查若在几秒内回滚并拉起旧版本，那一次启动还没过 600 秒，状态会暂时留在 `started`，要等以后再次启动面板才会记失败。

单实例锁：`/run/mihomo-auto-update.lock`（`/run` 不可写时用临时目录），`flock`。

## 面板在线升级

`upgrade_panel()` 的顺序：

1. `GET https://api.github.com/repos/<owner>/<name>/commits/<branch>`，拿到 40 位 commit 和 tree sha。API 失败就中止，不会去下未校验的 zip。
2. 再从 API 读该 commit 的 `remote-root/etc/mihomo/manager/app.py`，核对 git blob sha，解析 `PANEL_VERSION`。不比当前新则停。
3. `GET .../git/trees/<tree>?recursive=1`。树被截断也中止。
4. 下载 `archive/refs/heads/<branch>.zip` 对应的那次提交压缩包。候选地址是 GitHub 直连，失败后再用 `GH_PROXY`。zip 路径必须都落在 `<repo>-<commit>/` 下面。
5. 解压后按 git 树逐文件核对，再核对 `app.py` 的 sha256 和版本号，并 `python3 -m py_compile`。
6. `backup_panel_targets()` 把 `panel_managed_targets()` 拷进 `/etc/mihomo/backup/mihomo-panel.<YYYYmmddHHMMSS>/`，写 `manifest.json`。
7. 替换文件，`systemctl daemon-reload`，删掉超过 3 份的旧面板备份。这一步异常会立刻按清单还原。
8. 把**当前进程内存里**的 `panel_healthcheck.py` 写到 `/etc/mihomo/backup/panel-upgrade-healthcheck.py`，计划写到 `panel-upgrade-pending.json`。然后 `systemd-run` 启动它（失败则退化为独立 `Popen`）。等到出现 `panel-upgrade-health.armed`（最多约 8 秒；超时仍继续）后，`sleep 1` 再 `systemctl restart mihomo-manager`。

健康检查计划在重启前就读进那个独立进程：等旧 PID 消失（最多约 25 秒），再对 `panel_probe_url()`（由 `WEB_HOST` 和 `WEB_PORT` 拼出来，`0.0.0.0` 会改成 `127.0.0.1`）请求 `/login`。最多 12 次、间隔 2 秒，要连续 2 次 HTTP 200 且正文同时含 `name="password"` 和 `/login`。否则 `systemctl stop mihomo-manager`，按清单还原允许列表里的路径，`daemon-reload` 后再 start。

排除、永不覆盖的路径：`/etc/mihomo/.env`、`/etc/mihomo/config.yaml`、`/etc/mihomo/ui`。

## 通知

`notify_event(level, subject, lines, key=...)` 和 `notify.sh --event` 都进 `notify_format.py`。标题形如 `✅ 联通 · 订阅配置已更新`。级别：`ok` ✅、`warn` ⚠️、`fail` ❌、`info` 🔔。正文纯文本，最多几行；`notify.sh` 再追加北京时间。

带 `key` 时状态在 `/etc/mihomo/notify_state.json`：第一次失败发送；同一失败每满 3 天再提醒；恢复（同 key 的 `ok`/`info`）发一次。本来就正常则不发。

投递渠道在 `notify.sh`：Webhook（`NOTIFY_API` + `NOTIFY_API_URL`）和 Telegram（`NOTIFY_TG`、`TG_BOT_TOKEN`、`TG_CHAT_ID`）。Telegram token 经 curl 配置从 stdin 传入，不放进命令行。发送记录追加到 `/var/log/mihomo-notify.log`，面板通知中心解析最近 15 条。

会发通知的路径：订阅成功 / 下载失败 / 应用失败回滚；Geo 全失败或部分失败及恢复；自动更新的更新成功、失败、回滚。

## 设备、流量、爱快

`start_device_sampler()` 在面板进程里每 5 秒跑一次：

- `GET /connections`，按 `sourceIP` 把两次采样之间的字节差累加。结果定期写入 `/etc/mihomo/devices.json`（约 30 秒，以及收到 SIGTERM 时；`app.py` 把 SIGTERM 转成 `SystemExit` 才能跑到 atexit）。
- 同样的增量写入 `/etc/mihomo/traffic_daily.json`。换日用北京时间（UTC+8），因为容器时区经常是 UTC，而注释里写的几个站点都在国内。短于采样间隔的连接可能统计不到，分项之和可以小于总量。
- 每 30 秒读一次 `ip neigh`，用于没有流量、但在邻居表里的设备。
- 若配置了 `IKUAI_URL` 和 `IKUAI_TOKEN`，约 10 秒拉在线终端，约 300 秒拉离线和 DHCP 静态绑定。数据只放内存。爱快做了 SNAT 时，mihomo 看到的源地址会是路由器，设备页改以爱快终端为准，经路由器转发的流量单独算「全家合计」。

爱快请求路径是 `/api/v4.0/monitoring/clients-online`、`clients-offline`、`/api/v4.0/network/dhcp/static`、应用统计 `.../app-protocols/load`。成功码 `0` 或 `20000`。

`/api/site-info` 不登录，只返回 `SITE_NAME`，并加 `Access-Control-Allow-Origin: *`，给其他站点面板做切换菜单。

## 安全边界（代码里实际做了的）

- `.env` 用 `0600` 临时文件加 `os.replace`。
- 登录失败按 IP 限流；改密轮换 `WEB_SESSION_SECRET` 并清空当前 session。
- 密码优先 `werkzeug.security.generate_password_hash`，失败则用标准库 scrypt，格式仍是 `method$salt$digest`。
- 请求体上限 1 MiB。
- 规则同步和爱快 URL 有格式限制；爱快地址拒绝公网 IP。
- 面板升级和内核下载不信任代理上的摘要。`verify_github_asset.py` 自己不联网：代理下载没有可用 sha256 就拒绝；直连且 API 没给 sha256 时，调用方仍做 gzip 和可执行检查。
- 订阅 / Geo 默认校验 TLS 证书。

面板是 Flask 开发服务器，单进程，监听地址默认对容器网卡开放。它没有自己的 TLS，也没有单独的非 root 用户。
