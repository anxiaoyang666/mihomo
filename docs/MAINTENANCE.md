# 维护

命令都来自仓库里的单元文件和脚本。默认工作目录是 PVE LXC 内部，用 root。先看 [README.md](../README.md) 的配置表，改行为前看 [ARCHITECTURE.md](ARCHITECTURE.md)。

## systemd 单元

单元文件装在 `/etc/systemd/system/`，仓库副本在 `remote-root/etc/systemd/system/`。

```bash
systemctl status mihomo mihomo-manager force-ip-forward
systemctl restart mihomo              # 只动内核
systemctl restart mihomo-manager      # 只动面板。会断开当前网页，采样线程退出前写盘
systemctl restart mihomo mihomo-manager
```

CLI 等价物：`mihomo start`、`mihomo stop`、`mihomo restart` 同时操作 `mihomo-manager` 和 `mihomo`，不含 `force-ip-forward`。交互菜单第 2 项也是这两个服务。

| 单元 | 启动命令 | 失败时 |
| --- | --- | --- |
| `mihomo.service` | `/bin/bash -c "/usr/bin/mihomo-core -d /etc/mihomo > /var/log/mihomo.log 2>&1"` | `Restart=always`，3 秒后再拉。配置错误会打在日志里并循环重启 |
| `mihomo-manager.service` | `/usr/bin/python3 /etc/mihomo/manager/app.py` | 同样 `Restart=always`。Python 语法错误、端口占用会在 journal 里 |
| `force-ip-forward.service` | `/sbin/sysctl -w net.ipv4.ip_forward=1` | oneshot。失败通常是 LXC 不允许改 sysctl |

改过单元文件（升级会替换这三份）之后：

```bash
systemctl daemon-reload
systemctl restart mihomo-manager
```

内核单元不用在每次改 `config.yaml` 时 daemon-reload。面板保存配置时会自己决定热加载或 `systemctl restart mihomo`。

开机：

```bash
systemctl is-enabled mihomo mihomo-manager force-ip-forward
```

`install.sh` 会 enable `force-ip-forward` 和 `mihomo-manager`。`mihomo` 只在 `/usr/bin/mihomo-core` 和 `/etc/mihomo/config.yaml` 都存在时 enable。

### 日志分别在哪

| 内容 | 位置 |
| --- | --- |
| 内核标准输出 | `/var/log/mihomo.log`。`mihomo log` 或面板「运行日志」 |
| 面板进程（监听地址、登录失败、升级） | `journalctl -u mihomo-manager -n 100 --no-pager` |
| 内核单元的 systemd 记录（和上面的文件不是同一份） | `journalctl -u mihomo -n 50 --no-pager` |
| 订阅 | `/var/log/mihomo-subscription.log`，状态一行在 `/etc/mihomo/.last_subscription`（unix 时间、`ok`/`failed`、说明） |
| Geo | `/var/log/mihomo-geo.log` |
| 自动更新 | `/var/log/mihomo-auto-update.log`，状态 `/etc/mihomo/auto_update_state.json` |
| 通知发送结果 | `/var/log/mihomo-notify.log` |
| 面板升级健康检查 | `/var/log/mihomo-panel-upgrade-health.log` |

logrotate（`/etc/logrotate.d/mihomo`）每天轮转内核、通知、订阅、Geo、自动更新这五份，保留 7 份，`copytruncate`。健康检查日志不在这个列表里。

## 日常操作

在容器里：

```bash
mihomo                          # 菜单：内核、服务、订阅、日志、crontab、Geo、通知测试、网关、账号、卸载
bash /etc/mihomo/scripts/install_kernel.sh auto
bash /etc/mihomo/scripts/update_subscription.sh
bash /etc/mihomo/scripts/update_geo.sh
bash /etc/mihomo/scripts/gateway_init.sh          # 应用转发并写入每分钟 check
bash /etc/mihomo/scripts/gateway_init.sh check    # cron 用的静默模式
bash /etc/mihomo/scripts/notify.sh --event info "通知测试" "通知通道正常"
```

改 `.env` 的一个键（不要手写未引用的特殊字符）：

```bash
source /etc/mihomo/scripts/envutil.sh
upsert_env WEB_HOST 0.0.0.0
get_env WEB_PORT
systemctl restart mihomo-manager
```

校验配置而不重启：

```bash
/usr/bin/mihomo-core -t -d /etc/mihomo -f /etc/mihomo/config.yaml
/usr/bin/mihomo-core -v
```

看定时任务：

```bash
crontab -l | grep -E 'JOB_SUB|JOB_GEO|MIHOMO_AUTO_UPDATE|gateway_init'
```

自动更新可以只检查、不安装：

```bash
python3 /etc/mihomo/manager/auto_update.py --dry-run
python3 /etc/mihomo/manager/auto_update.py --manual --only core
python3 /etc/mihomo/manager/auto_update.py --manual --only ui
python3 /etc/mihomo/manager/auto_update.py --manual --only panel
```

`--manual` 在 `.env` 把自动更新关掉时也会执行。不加它、且 `AUTO_UPDATE_ENABLED=false` 时脚本直接退出。`--json` 让面板解析最后一行。正在跑时第二个人会拿到退出码 3（锁被占用）。

面板上的对应按钮：概览里的启停、更新订阅、初始化网关、升级面板；订阅页的保存并应用、配置文本框、定时任务和自动更新；通知页的测试。测试通知读的是已经写入 `.env` 的渠道，页面上还没保存的 URL 不会被测到（前端会先提示保存）。

## 升级与回滚

### 面板（推荐）

概览里升级，或等 `# MIHOMO_AUTO_UPDATE`。升级前确认容器能访问 `api.github.com`（钉提交）以及 `github.com` 或 `.env` 里的 `GH_PROXY`（下 zip）。升级后浏览器应在几秒内重新打开登录页。

看结果：

```bash
journalctl -u mihomo-manager -n 80 --no-pager
tail -n 50 /var/log/mihomo-panel-upgrade-health.log
ls -d /etc/mihomo/backup/mihomo-panel.*
```

健康检查认为失败时会自己还原。它没启动成功时，升级接口的返回文字里会带备份目录，需要人工还原。备份里的 `manifest.json` 列出每个目标路径、是文件还是目录、升级前是否存在。允许还原的路径就是这三个单元、logrotate、`/usr/bin/mihomo`、`/etc/mihomo/manager`、`scripts`、`templates`、`config.example.yaml`。

人工把某一份面板备份还原（把时间戳换成 `ls` 看到的目录名）：

```bash
systemctl stop mihomo-manager
BACKUP=/etc/mihomo/backup/mihomo-panel.YYYYMMDDHHMMSS
python3 - "$BACKUP" <<'PY'
import json, os, shutil, sys
root = sys.argv[1]
manifest = json.load(open(os.path.join(root, "manifest.json"), encoding="utf-8"))
for item in manifest:
    target = item["target"]
    src = os.path.join(root, target.lstrip("/"))
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    elif os.path.lexists(target):
        os.remove(target)
    if not item.get("existed"):
        continue
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if item.get("kind") == "dir":
        shutil.copytree(src, target, symlinks=False)
    else:
        shutil.copy2(src, target)
PY
systemctl daemon-reload
systemctl start mihomo-manager
```

不要把 `.env`、`config.yaml`、`ui/` 从面板备份里找回来：升级根本没把它们放进备份。

只留最近 3 份 `mihomo-panel.*`，更旧的会在下一次成功替换后删掉。还原前先把要留的那份拷到 `/etc/mihomo` 外面。

旧安装没有升级按钮时，用 README 里的一次性命令。那条命令只额外备份了当时的 `manager/` 到 `/etc/mihomo/backup/manager.before-upgrade.<时间戳>/`，回滚就是把该目录拷回 `/etc/mihomo/manager` 并重启 `mihomo-manager`。它不会自动恢复同一次命令替换掉的脚本、单元和 CLI；那些要从 git 历史或另一份备份拿。

### 内核

自动更新的备份：

```bash
ls -l /etc/mihomo/backup/mihomo-core.*
```

文件名是 `mihomo-core.<旧版本>.<YYYYmmddHHMMSS>`。换回某一份：

```bash
install -m 0755 /etc/mihomo/backup/mihomo-core.v1.19.0.YYYYMMDDHHMMSS /usr/bin/mihomo-core
systemctl restart mihomo
/usr/bin/mihomo-core -v
```

版本号以 `ls` 和 `mihomo-core -v` 为准，上面的 `v1.19.0` 只是文件名形状。自动更新最多保留 3 份。`install_kernel.sh` 不写这种备份：它在确认新文件能 `-v` 之后用 `mv` 原子替换 `/usr/bin/mihomo-core`。手动更新前如果要留后路，先自己 `cp -a /usr/bin/mihomo-core /etc/mihomo/backup/`。

`install_kernel.sh` 在服务正 active 时会重启 `mihomo`，没在跑则不启动。已安装版本比 GitHub 最新稳定版还新时拒绝降级。

### 配置

订阅和面板保存配置时会把旧文件拷成：

- `/etc/mihomo/backup/config_<YYYYmmddHHMMSS>.yaml`（面板保存、订阅替换）
- `/etc/mihomo/backup/config.before-rule-sync.<时间戳>.yaml`（规则同步改写配置时）

份数由 `BACKUP_KEEP_COUNT` 限制。订阅应用失败时脚本自己会拷回并重启；人要再退一版：

```bash
cp -a /etc/mihomo/backup/config_YYYYMMDDHHMMSS.yaml /etc/mihomo/config.yaml
systemctl restart mihomo
```

节点订阅模式下次跑 `update_subscription.sh` 会按模板重新生成，手改的 `config.yaml` 会被盖掉。要留的改动放进 `templates/default.yaml`（面板升级会替换这份模板）或改用 `CONFIG_MODE=raw`。

### Dashboard（`/etc/mihomo/ui`）

由内核 `POST /upgrade/ui` 下载，地址在 `config.yaml` 的 `external-ui-url`。示例配置指向 Zashboard 的 gh-pages zip，并带了 `gh-proxy.com` 前缀。更新失败时 `auto_update.py` 会把更新前的目录移回。没有单独的长期备份目录。

## 网关和 LXC

这套部署把容器当网关：自带模板开启 TUN、`allow-lan`、DNS 监听 `0.0.0.0:53`、控制器 `0.0.0.0:9090`。PVE 上要让这只 LXC 具备：

- `/dev/net/tun`（模板 `tun.enable: true`；没有 TUN 时内核会起不来或 TUN 入站失败）
- 允许 `sysctl` 改 `net.ipv4.ip_forward` 和 `rp_filter`
- 允许 `iptables` 改 filter 表 FORWARD 的默认策略，以及 nat 表 POSTROUTING 的 MASQUERADE

仓库不附带 PVE 宿主机配置。容器里可以核对脚本想要的状态：

```bash
sysctl net.ipv4.ip_forward
sysctl net.ipv4.conf.all.rp_filter
iptables -S FORWARD | head
iptables -t nat -S POSTROUTING
ip route show default
cat /etc/sysctl.d/99-mihomo-gateway.conf
```

`gateway_init.sh` 用默认路由的出口网卡做 MASQUERADE，读不到路由时用 `eth0`。它只在当前值不对时才写。`GATEWAY_AUTOFIX=false` 让每分钟的 `check` 什么都不做，手动执行仍会改。

`force-ip-forward.service` 不管 MASQUERADE。只开了这个单元、没跑过 `gateway_init.sh` 时，转发开关是开的，伪装和 FORWARD 策略不一定对。

## 账号和密钥

忘了面板密码：向 `.env` 写回明文，重启后面板会再哈希。

```bash
source /etc/mihomo/scripts/envutil.sh
upsert_env WEB_SECRET '新的密码'
systemctl restart mihomo-manager
```

用户名空或文件里没有 `WEB_USER` / `WEB_SECRET` 时，登录直接拒绝。

内核认的是 `config.yaml` 里的 `secret`，面板认的是 `MIHOMO_API_SECRET`（没有则再读配置）。两边要一致。只改 `.env` 不会改内核已经加载的密钥；改 `config.yaml` 的 `secret` 会触发重启（它在需要重启的键里）。外部 Dashboard 的密钥就是这个值。CLI 菜单第 9 项会打印 `MIHOMO_API_SECRET` 和 `WEB_SECRET`；密码变成哈希之后，菜单里看到的是哈希而不是原密码。

`RULE_SYNC_TOKEN` 泄露就等于别人可以改强制直连 / 强制代理名单。在同步设置页重新生成并保存，然后把新值配到每个对端。

## 订阅和 Geo 失败时

订阅脚本非 0 退出会把 `.last_subscription` 写成 `failed`。常见原因：

- `CONFIG_MODE=raw` 但 `SUB_URL_RAW` 空：脚本打印跳过并以 0 退出（不算失败）。
- wget 失败：检查容器到订阅地址的网络。只有确认对端证书有问题才临时设 `ALLOW_INSECURE_TLS=true`。
- `mihomo-core -t` 没通过：还没覆盖 `config.yaml`，旧配置仍在用。
- 已经换成新配置但启动失败：脚本把本次 `config_*.yaml` 拷回去再重启。看订阅日志末尾和 `/var/log/mihomo.log`。
- 机场返回了 base64 节点而不是 YAML：`SUB_USER_AGENT` 默认是 `clash.meta`，不要随便改成浏览器 UA。

Geo（`update_geo.sh`）做两件事：把 `geoip.dat`、`geosite.dat`、`geoip.metadb` 从 `MetaCubeX/meta-rules-dat` 的 latest 下载到临时目录，内容变了才替换，替换了才重启内核；另外对正在运行的内核 `PUT /providers/rules/<name>` 刷新 `config.yaml` 里的 rule-providers。三个文件都失败则不重启、退出码 1，并发一条带 `--key geo` 的通知。

## 卸载

```bash
bash /etc/mihomo/scripts/uninstall.sh              # 会询问
bash /etc/mihomo/scripts/uninstall.sh -y --keep-data
bash /etc/mihomo/scripts/uninstall.sh -y --purge   # 删除整个 /etc/mihomo
```

会停止并 disable 三个单元、删除单元文件、`daemon-reload`、清掉本项目的 crontab、删除 `/etc/sysctl.d/99-mihomo-gateway.conf`、`/etc/logrotate.d/mihomo`、`/usr/bin/mihomo`、`/usr/bin/mihomo-core`、`/usr/bin/mihomo-core.new`，以及订阅 / Geo / 自动更新日志和 `/run/mihomo-auto-update.lock`。iptables 里已经加上的 MASQUERADE 不会自动删。`--purge` 才会 `rm -rf /etc/mihomo`。

## 改仓库时

- 运行时代码在 `remote-root/`。改完要用安装或面板升级才会进 LXC；在开发机上跑 pytest 不会影响容器。
- 改 `.env` 的引用格式时，同时改 `install.sh` 的 `env_upsert` / `env_get` 和 `scripts/envutil.sh`。面板的 `write_env` / `parse_env_line` 是 Python 侧的同一套规则。
- 改内核平台名或下载 URL 时，同时看 `install_kernel.sh` 和 `auto_update.py` 的 `CORE_PLATFORMS`。
- 改本地网段规则时，同时看 `app.py` 的 `normalize_local_networks` 和 `update_subscription.sh` 里标记 `BEGIN_LOCAL_NETWORKS_FALLBACK` 的那份副本。
- 新增 `scripts/*.sh` 必须被 `install.sh`、CLI、面板、systemd、README 或其他活脚本点到名字，否则 `tests/test_script_stability_contract.py` 失败。
- 改面板升级会替换的文件列表时，同时改 `panel_managed_targets()` 和 README 里的一次性升级命令。
- 发新版本：改 `PANEL_VERSION`。在线升级只在远端版本号更高时安装。

本地：

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/
shellcheck --severity=warning install.sh remote-root/etc/mihomo/scripts/*.sh
```
