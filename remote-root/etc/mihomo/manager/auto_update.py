#!/usr/bin/env python3
"""Mihomo 服务端自动更新（cron 每天调用一次：python3 /etc/mihomo/manager/auto_update.py）。

一次运行按顺序处理：面板 UI（到间隔才更新）→ mihomo 内核 → 管理面板（最后，升级会替换 manager 目录并重启 mihomo-manager）。

内核更新是最危险的一步：远程站点只能通过本机 mihomo 的入站访问，内核坏了就只能去现场。所以：
只装发布满 N 天的稳定版、绝不降级；新内核先 -v 自检、再用它校验当前配置；备份旧内核后原子替换并重启；
30 秒内检查服务状态、控制器版本、DNS 解析、所有入站端口都在监听，任一不通过立刻换回旧内核。

用法：auto_update.py [--dry-run] [--only core|ui|panel] [--json] [--manual]
  --dry-run  只检查并报告，不安装、不改任何东西（状态文件只记录检查到的版本）
  --manual   面板“立即检查/立即更新”调用；没有它时，.env 里关闭了自动更新就直接退出
"""
import argparse
import gzip
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CORE_BIN = "/usr/bin/mihomo-core"
CORE_REPO = "MetaCubeX/mihomo"
CORE_RELEASES_API = f"https://api.github.com/repos/{CORE_REPO}/releases?per_page=30"
# 和 scripts/install_kernel.sh 的架构检测保持一致
CORE_PLATFORMS = {
    "x86_64": "linux-amd64-compatible",
    "amd64": "linux-amd64-compatible",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
}
CORE_BACKUP_KEEP = 3
CORE_MAX_BYTES = 256 * 1024 * 1024
HEALTH_TIMEOUT = 30
HEALTH_INTERVAL = 2
DNS_TEST_NAME = "www.google.com"
# cron 每天同一时刻触发，执行时长会让“距上次成功”略少于整数天，留 1 小时余量
INTERVAL_SLACK_SECONDS = 3600
DAY = 86400
STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
NOTIFY_RESULTS = {"updated", "failed", "rolled_back"}
EXIT_BUSY = 3


def load_panel():
    """导入同目录的 app.py。只用它的函数，不启动 Flask，也不启动设备采样线程（那些只在 __main__ 里）。"""
    import app as panel_module
    return panel_module


def version_tuple(value):
    match = VERSION_RE.search(str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def version_text(value):
    parts = version_tuple(value)
    return "v" + ".".join(str(p) for p in parts) if parts else ""


def parse_github_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def format_days(seconds):
    return f"{seconds / DAY:.1f}"


# ------------------------------------------------------------------
# 运行环境：所有会碰系统/网络的动作都挂在这里，测试里整体换成假的
# ------------------------------------------------------------------
def default_run(args, timeout=60):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout} 秒）：{' '.join(args)}"
    except Exception as e:
        return 127, str(e)


def build_dns_query(name, qid, qtype=1):
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.strip(".").split("."))
    return header + labels + b"\x00" + struct.pack(">HH", qtype, 1)


def parse_dns_answer_count(data, qid):
    """返回应答记录数；不是对应查询的应答、或 RCODE 非 0 时返回 (0, 说明)。"""
    if len(data) < 12:
        return 0, "应答太短"
    rid, flags, _, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != qid or not flags & 0x8000:
        return 0, "应答 ID 不匹配"
    rcode = flags & 0x000F
    if rcode != 0:
        return 0, f"RCODE={rcode}"
    return ancount, "" if ancount else "没有应答记录"


def default_dns_probe(host, port, name, timeout=3):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    qid = int.from_bytes(os.urandom(2), "big")
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(build_dns_query(name, qid), (host, port))
            data, _ = sock.recvfrom(4096)
    except Exception as e:
        return False, f"DNS {host}:{port} 查询 {name} 失败：{e}"
    count, detail = parse_dns_answer_count(data, qid)
    if count <= 0:
        return False, f"DNS {host}:{port} 查询 {name} 无结果：{detail}"
    return True, f"DNS {host}:{port} 解析 {name} 成功"


def parse_proc_net_tcp(text):
    ports = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 3 and parts[3] == "0A":  # 0A = LISTEN
            try:
                ports.add(int(parts[1].rsplit(":", 1)[1], 16))
            except (IndexError, ValueError):
                pass
    return ports


def default_listening_ports():
    ports = set()
    found = False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="ascii", errors="replace") as f:
                ports |= parse_proc_net_tcp(f.read())
            found = True
        except OSError:
            pass
    if found:
        return ports
    rc, output = default_run(["ss", "-ltnH"], timeout=10)
    for line in output.splitlines() if rc == 0 else []:
        parts = line.split()
        if len(parts) >= 4:
            match = re.search(r":(\d+)$", parts[3])
            if match:
                ports.add(int(match.group(1)))
    return ports


class Runtime:
    """一次运行用到的路径和副作用函数。测试里传 overrides 替换。"""

    def __init__(self, panel, **overrides):
        self.panel = panel
        self.core_bin = CORE_BIN
        self.mihomo_dir = panel.MIHOMO_DIR
        self.config_file = panel.CONFIG_FILE
        self.backup_dir = panel.BACKUP_DIR
        self.state_file = panel.AUTO_UPDATE_STATE_FILE
        self.log_file = panel.AUTO_UPDATE_LOG
        self.lock_path = panel.auto_update_lock_path()
        self.now = time.time
        self.sleep = time.sleep
        self.machine = platform.machine
        self.run = default_run
        self.fetch_text = lambda urls, timeout=20: panel.read_url_text(urls, timeout=timeout, max_bytes=8 * 1024 * 1024)
        self.download = lambda urls, output: panel.download_file(urls, output, timeout=120)
        self.service_active = panel.is_service_active
        self.controller_version = self._controller_version
        self.controller_request = self._controller_request
        self.dns_probe = default_dns_probe
        self.listening_ports = default_listening_ports
        self.notify = self._notify
        self.upgrade_panel = panel.upgrade_panel
        self.health_timeout = HEALTH_TIMEOUT
        self.health_interval = HEALTH_INTERVAL
        self.echo = print
        for key, value in overrides.items():
            setattr(self, key, value)

    def _controller_version(self):
        ok, data = self.panel.mihomo_api_get("/version", timeout=3)
        if not ok or not isinstance(data, dict):
            return ""
        return str(data.get("version") or "")

    def _controller_request(self, method, path, timeout=60):
        """返回 (HTTP 状态码或 0, 说明)。"""
        settings = self.panel.mihomo_controller_settings()
        headers = {"User-Agent": "mihomo-auto-update"}
        if settings.get("secret"):
            headers["Authorization"] = "Bearer " + settings["secret"]
        data = b"" if method in ("POST", "PUT") else None
        try:
            req = urlrequest.Request(settings["base_url"] + path, data=data, headers=headers, method=method)
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                return int(getattr(resp, "status", None) or resp.getcode()), ""
        except urlerror.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace").strip()[:200]
            except Exception:
                pass
            return int(e.code), detail
        except Exception as e:
            return 0, str(e)

    def _notify(self, title, content):
        script = os.path.join(self.panel.SCRIPT_DIR, "notify.sh")
        if os.path.exists(script):
            self.run(["bash", script, title, content], timeout=60)


# ------------------------------------------------------------------
# 状态与日志
# ------------------------------------------------------------------
def load_state(rt):
    return rt.panel.read_auto_update_state(rt.state_file)


def save_state(rt, state):
    rt.panel.write_auto_update_state(state, rt.state_file)


def write_log(rt, item, message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rt.now()))
    line = f"[{stamp}] [{item}] {message}"
    try:
        os.makedirs(os.path.dirname(rt.log_file), exist_ok=True)
        with open(rt.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


def make_result(item, result, message, current="", latest="", from_version="", to_version=""):
    return {"item": item, "result": result, "message": message, "current": current, "latest": latest,
            "from": from_version, "to": to_version}


def record(rt, state, report, dry_run):
    """把一项结果写进状态。dry-run 只更新检查时间和看到的版本，不覆盖上一次真实结果。"""
    item = state["items"].setdefault(report["item"], {})
    now = int(rt.now())
    item["last_check"] = now
    for key in ("current", "latest"):
        if report.get(key):
            item[key] = report[key]
    if dry_run:
        item["last_dry_run"] = {"time": now, "result": report["result"], "message": report["message"]}
        return
    if report["result"] == "not_due":
        item["next_due"] = report.get("next_due")
        return
    item.update({"last_result": report["result"], "from": report.get("from", ""), "to": report.get("to", ""),
                 "message": report["message"], "last_run": now})
    for key in ("started_at", "last_success"):
        if report.get(key):
            item[key] = report[key]


def notify_result(rt, report):
    if report["result"] not in NOTIFY_RESULTS:
        return
    labels = rt.panel.AUTO_UPDATE_ITEM_LABELS
    results = rt.panel.AUTO_UPDATE_RESULT_LABELS
    change = f" {report['from']} → {report['to']}" if report.get("from") or report.get("to") else ""
    title = f"Mihomo 自动更新（{socket.gethostname()}）"
    content = f"{labels.get(report['item'], report['item'])}：{results.get(report['result'], report['result'])}{change}\n{report['message']}"
    try:
        rt.notify(title, content)
    except Exception as e:
        write_log(rt, report["item"], f"发送通知失败：{e}")


# ------------------------------------------------------------------
# GitHub
# ------------------------------------------------------------------
def github_api_candidates(rt, url):
    """api.github.com 也按“先直连、再 GH_PROXY”的顺序。"""
    urls = [url]
    prefix = rt.panel.github_proxy_prefix()
    if prefix:
        urls.append(prefix + url)
    return urls


def fetch_json(rt, url):
    ok, text, _ = rt.fetch_text(github_api_candidates(rt, url), timeout=20)
    if not ok:
        return False, f"请求失败：{text}"
    try:
        return True, json.loads(text)
    except ValueError:
        return False, "返回的不是 JSON"


def pick_core_release(releases, min_age_days, now):
    """返回 (可安装的最新稳定版 tag 或 "", 最新稳定版 tag, 说明)。预发布/草稿/非 vX.Y.Z 的 tag 一律不要。"""
    stable = []
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "")
        published = parse_github_time(release.get("published_at"))
        if not STABLE_TAG_RE.match(tag) or published is None:
            continue
        stable.append((version_tuple(tag), tag, published))
    if not stable:
        return "", "", "没有找到稳定版 release"
    stable.sort(reverse=True)
    newest_tag, newest_published = stable[0][1], stable[0][2]
    for _, tag, published in stable:
        if now - published >= min_age_days * DAY:
            note = "" if tag == newest_tag else f"最新稳定版 {newest_tag} 发布 {format_days(now - newest_published)} 天，不足 {min_age_days} 天"
            return tag, newest_tag, note
    return "", newest_tag, f"最新稳定版 {newest_tag} 发布 {format_days(now - newest_published)} 天，不足 {min_age_days} 天"


def core_asset(tag, machine):
    platform_name = CORE_PLATFORMS.get(str(machine or "").lower())
    if not platform_name:
        return "", ""
    name = f"mihomo-{platform_name}-{tag}.gz"
    return name, f"https://github.com/{CORE_REPO}/releases/download/{tag}/{name}"


def panel_latest_commit_time(rt, settings):
    """remote-root/ 在该分支上最新一次提交的时间（epoch）；拿不到返回 (None, 原因)。"""
    parts = rt.panel.github_repo_parts(settings["repo_url"])
    if not parts:
        return None, "只支持 GitHub 仓库"
    owner, name = parts
    url = f"https://api.github.com/repos/{owner}/{name}/commits?path=remote-root&sha={quote(settings['branch'], safe='')}&per_page=1"
    ok, data = fetch_json(rt, url)
    if not ok:
        return None, f"GitHub 提交记录不可用（{data}）"
    try:
        stamp = parse_github_time(data[0]["commit"]["committer"]["date"])
    except (KeyError, IndexError, TypeError):
        stamp = None
    if stamp is None:
        return None, "GitHub 提交记录格式无法识别"
    return stamp, ""


# ------------------------------------------------------------------
# 配置里和健康检查有关的部分
# ------------------------------------------------------------------
def int_port(value):
    try:
        port = int(str(value).strip().strip("'\""))
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def read_health_targets(rt):
    """返回 {"dns": (host, port) 或 None, "ports": {端口...}}。优先用 PyYAML，没有时按行解析。"""
    try:
        with open(rt.config_file, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    data = None
    yaml_module = getattr(rt.panel, "yaml", None)
    if yaml_module is not None:
        try:
            data = yaml_module.safe_load(text)
        except Exception:
            data = None
    if isinstance(data, dict):
        dns = data.get("dns") if isinstance(data.get("dns"), dict) else {}
        dns_enabled = dns.get("enable", False) is True or str(dns.get("enable")).lower() == "true"
        dns_listen = str(dns.get("listen") or "") if dns_enabled else ""
        ports = set()
        if int_port(data.get("mixed-port")):
            ports.add(int_port(data.get("mixed-port")))
        for listener in data.get("listeners") or [] if isinstance(data.get("listeners"), list) else []:
            if isinstance(listener, dict) and int_port(listener.get("port")):
                ports.add(int_port(listener.get("port")))
    else:
        dns_enabled, dns_listen, ports = fallback_health_targets(text)
    dns_target = parse_listen(dns_listen or "127.0.0.1:53") if dns_enabled else None
    return {"dns": dns_target, "ports": ports}


def fallback_health_targets(text):
    lines = text.splitlines()
    ports = set()
    dns_enabled, dns_listen = False, ""
    section = ""
    child_indent = None
    for line in lines:
        stripped = line.split(" #", 1)[0].rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        if not line[0].isspace() and not line.startswith("-"):
            key, _, value = stripped.partition(":")
            section, child_indent = key.strip(), None
            if section == "mixed-port" and int_port(value):
                ports.add(int_port(value))
            continue
        if section == "dns":
            indent = len(line) - len(line.lstrip(" "))
            child_indent = indent if child_indent is None else child_indent
            if indent != child_indent:
                continue
            key, _, value = stripped.strip().partition(":")
            value = value.strip().strip("'\"")
            if key == "enable":
                dns_enabled = value.lower() == "true"
            elif key == "listen":
                dns_listen = value
        elif section == "listeners":
            for match in re.finditer(r"(?:^|[\s{,])port\s*:\s*['\"]?(\d+)", stripped.lstrip(" -")):
                if int_port(match.group(1)):
                    ports.add(int_port(match.group(1)))
    return dns_enabled, dns_listen, ports


def parse_listen(value):
    value = str(value or "").strip().strip("'\"")
    match = re.match(r"^\[([^\]]*)\]:(\d+)$", value) or re.match(r"^([^:]*):(\d+)$", value)
    if not match:
        return ("127.0.0.1", 53)
    host, port = match.group(1), int(match.group(2))
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    return (host, port)


def health_check(rt, expected_version, targets):
    """在 health_timeout 秒内反复检查，全部通过返回 (True, [])，否则返回最后一次的失败项。"""
    deadline = rt.now() + rt.health_timeout
    failures = []
    while True:
        failures = []
        if not rt.service_active("mihomo"):
            failures.append("systemctl is-active mihomo 不是 active")
        else:
            reported = version_text(rt.controller_version())
            if not reported:
                failures.append("控制器 /version 没有响应")
            elif reported != version_text(expected_version):
                failures.append(f"控制器报告版本 {reported}，期望 {version_text(expected_version)}")
            if targets.get("dns"):
                host, port = targets["dns"]
                ok, detail = rt.dns_probe(host, port, DNS_TEST_NAME)
                if not ok:
                    failures.append(detail)
            required = set(targets.get("ports") or ())
            if required:
                missing = sorted(required - set(rt.listening_ports() or ()))
                if missing:
                    failures.append("入站端口没有在监听：" + ", ".join(str(p) for p in missing))
        if not failures:
            return True, []
        if rt.now() >= deadline:
            return False, failures
        rt.sleep(rt.health_interval)


# ------------------------------------------------------------------
# mihomo 内核
# ------------------------------------------------------------------
def binary_version(rt, path):
    if not os.path.exists(path):
        return ""
    rc, output = rt.run([path, "-v"], timeout=15)
    return version_text(output) if rc == 0 else ""


def prune_core_backups(rt):
    backups = [p for p in os.listdir(rt.backup_dir) if p.startswith("mihomo-core.")] if os.path.isdir(rt.backup_dir) else []
    # 文件名末尾是时间戳 mihomo-core.<版本>.<YYYYmmddHHMMSS>
    backups.sort(key=lambda name: name.rsplit(".", 1)[-1], reverse=True)
    for name in backups[CORE_BACKUP_KEEP:]:
        try:
            os.remove(os.path.join(rt.backup_dir, name))
        except OSError:
            pass


def replace_core(rt, source):
    """先拷到 /usr/bin 下的临时名再 rename，替换是原子的；正在运行的进程继续用旧 inode。"""
    staging = rt.core_bin + ".new"
    shutil.copyfile(source, staging)
    os.chmod(staging, 0o755)
    os.replace(staging, rt.core_bin)


def decompress_core(gz_path, output):
    """解压同时校验 gzip（CRC/截断都会抛异常），相当于 gzip -t。"""
    written = 0
    with gzip.open(gz_path, "rb") as src, open(output, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > CORE_MAX_BYTES:
                raise ValueError("解压后超过大小上限")
            dst.write(chunk)
    if written == 0:
        raise ValueError("解压后文件为空")
    os.chmod(output, 0o755)


def update_core(rt, settings, dry_run):
    current = binary_version(rt, rt.core_bin)
    if not current:
        return make_result("core", "skipped", f"{rt.core_bin} 不存在或无法执行，自动更新不负责首次安装")
    ok, releases = fetch_json(rt, CORE_RELEASES_API)
    if not ok:
        return make_result("core", "skipped", f"无法获取 mihomo 发布列表，跳过：{releases}", current=current)
    tag, newest, note = pick_core_release(releases, settings["core_min_age_days"], rt.now())
    if not tag:
        return make_result("core", "up_to_date", note or "没有可安装的稳定版", current=current)
    if version_tuple(tag) <= version_tuple(current):
        message = f"当前 {current}，满足条件的最新稳定版 {tag}"
        if version_tuple(tag) < version_tuple(current):
            message += "，不降级"
        return make_result("core", "up_to_date", message + (f"；{note}" if note else ""), current=current, latest=tag)
    asset_name, url = core_asset(tag, rt.machine())
    if not url:
        return make_result("core", "failed", f"不支持的架构：{rt.machine()}", current=current, latest=tag)
    if dry_run:
        return make_result("core", "available", f"可以从 {current} 更新到 {tag}（{asset_name}）" + (f"；{note}" if note else ""),
                           current=current, latest=tag, from_version=current, to_version=tag)

    targets = read_health_targets(rt)
    workdir = tempfile.mkdtemp(prefix=".auto-update.", dir=rt.mihomo_dir)  # /tmp 可能 noexec，放在 /etc/mihomo 下
    try:
        gz_path = os.path.join(workdir, asset_name)
        new_bin = os.path.join(workdir, "mihomo-core")
        ok, detail = rt.download(rt.panel.github_candidate_urls(url), gz_path)
        if not ok:
            return make_result("core", "failed", f"下载 {asset_name} 失败：{detail}", current, tag, current, tag)
        try:
            decompress_core(gz_path, new_bin)
        except Exception as e:
            return make_result("core", "failed", f"压缩包校验失败：{e}", current, tag, current, tag)
        reported = binary_version(rt, new_bin)
        if reported != tag:
            return make_result("core", "failed", f"新内核 -v 报告 {reported or '无法执行'}，期望 {tag}，未做改动", current, tag, current, tag)
        rc, output = rt.run([new_bin, "-t", "-d", rt.mihomo_dir, "-f", rt.config_file], timeout=60)
        if rc != 0:
            return make_result("core", "failed", f"新内核 {tag} 校验当前配置失败，未做改动：\n{output.strip()[-1500:]}", current, tag, current, tag)

        os.makedirs(rt.backup_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(rt.now()))
        backup = os.path.join(rt.backup_dir, f"mihomo-core.{current}.{stamp}")
        shutil.copyfile(rt.core_bin, backup)
        os.chmod(backup, 0o755)
        prune_core_backups(rt)
        write_log(rt, "core", f"已备份 {current} 到 {backup}，开始替换为 {tag}")

        try:
            replace_core(rt, new_bin)
            rt.run(["systemctl", "restart", "mihomo"], timeout=60)
            healthy, failures = health_check(rt, tag, targets)
        except Exception as e:
            healthy, failures = False, [f"替换/重启出错：{e}"]
        if healthy:
            return make_result("core", "updated", f"已从 {current} 更新到 {tag}，健康检查通过", current=tag, latest=tag,
                               from_version=current, to_version=tag)
        return rollback_core(rt, backup, current, tag, targets, failures)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def rollback_core(rt, backup, current, tag, targets, failures):
    write_log(rt, "core", f"{tag} 健康检查失败：{'；'.join(failures)}，回滚到 {current}")
    try:
        replace_core(rt, backup)
        rt.run(["systemctl", "restart", "mihomo"], timeout=60)
        healthy, after = health_check(rt, current, targets)
    except Exception as e:
        healthy, after = False, [f"回滚出错：{e}"]
    message = f"{tag} 健康检查失败（{'；'.join(failures)}），已回滚到 {current}"
    if healthy:
        message += "，回滚后检查通过"
    else:
        message += f"。回滚后检查仍未通过（{'；'.join(after)}），需要人工处理！"
    return make_result("core", "rolled_back", message, current=current, latest=tag, from_version=current, to_version=tag)


# ------------------------------------------------------------------
# 面板 UI（zashboard）：控制器 POST /upgrade/ui，不重启 mihomo
# ------------------------------------------------------------------
def ui_dir(rt):
    value = rt.panel.config_value("external-ui")
    if not value:
        return ""
    return value if os.path.isabs(value) else os.path.join(rt.mihomo_dir, value)


def update_ui(rt, settings, state, dry_run):
    interval = settings["ui_interval_days"]
    last_success = int(state["items"].get("ui", {}).get("last_success") or 0)
    next_due = last_success + interval * DAY - INTERVAL_SLACK_SECONDS
    if last_success and rt.now() < next_due:
        due_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(next_due))
        report = make_result("ui", "not_due", f"距上次更新不足 {interval} 天，{due_text} 之后再更新")
        report["next_due"] = int(next_due)
        return report
    target = ui_dir(rt)
    if not target:
        return make_result("ui", "skipped", "config.yaml 没有配置 external-ui，跳过")
    if dry_run:
        return make_result("ui", "available", f"已到更新间隔（{interval} 天），将通过控制器重新下载面板 UI")

    workdir = tempfile.mkdtemp(prefix=".ui-backup.", dir=rt.mihomo_dir)
    backup = os.path.join(workdir, "ui")
    try:
        had_ui = os.path.isdir(target)
        if had_ui:
            shutil.copytree(target, backup, symlinks=True)
        status, detail = rt.controller_request("POST", "/upgrade/ui", timeout=120)
        problems = []
        if status not in (200, 204):
            problems.append(f"POST /upgrade/ui 返回 {status or '无响应'} {detail}".strip())
        else:
            check_status, check_detail = rt.controller_request("GET", "/ui/", timeout=15)
            if check_status != 200:
                problems.append(f"GET /ui/ 返回 {check_status or '无响应'} {check_detail}".strip())
            if not os.path.isfile(os.path.join(target, "index.html")):
                problems.append("更新后 index.html 不存在")
        if not problems:
            report = make_result("ui", "updated", "面板 UI 已重新下载，/ui/ 可以访问")
            report["last_success"] = int(rt.now())
            return report
        if had_ui:
            shutil.rmtree(target, ignore_errors=True)
            shutil.move(backup, target)
            return make_result("ui", "rolled_back", "面板 UI 更新失败，已恢复原文件：" + "；".join(problems))
        return make_result("ui", "failed", "面板 UI 更新失败：" + "；".join(problems))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------------
# 管理面板：复用 app.upgrade_panel()（自带备份和回滚），最后执行
# ------------------------------------------------------------------
def check_panel(rt, settings):
    panel = rt.panel
    current = panel.PANEL_VERSION
    remote = panel.remote_panel_version()
    if not remote.get("success"):
        return make_result("panel", "skipped", f"无法获取远端面板版本，跳过：{remote.get('message', '')}", current=current)
    latest = remote.get("latest_version", "")
    if not version_tuple(latest) or version_tuple(latest) <= version_tuple(current):
        return make_result("panel", "up_to_date", f"当前 v{current}，远端 v{latest}", current=current, latest=latest)
    min_age = settings["panel_min_age_days"]
    if min_age > 0:
        stamp, reason = panel_latest_commit_time(rt, panel.panel_repo_settings())
        if stamp is None:
            # 拿不到提交时间就不升级，绝不放行
            return make_result("panel", "skipped", f"{reason}，无法确认提交已满 {min_age} 天，跳过", current=current, latest=latest)
        age = rt.now() - stamp
        if age < min_age * DAY:
            return make_result("panel", "skipped", f"v{latest} 的最新提交距今 {format_days(age)} 天，不足 {min_age} 天", current=current, latest=latest)
    return make_result("panel", "available", f"可以从 v{current} 升级到 v{latest}", current=current, latest=latest,
                       from_version=current, to_version=latest)


def run_panel_upgrade(rt, state, settings):
    panel = rt.panel
    check = check_panel(rt, settings)
    if check["result"] != "available":
        return check
    current, latest = check["from"], check["to"]
    # 升级成功后本进程所在的代码会被替换、mihomo-manager 会重启：先把“已开始”落盘，新面板启动时改成最终结果
    started = make_result("panel", "started", f"面板升级已开始：v{current} → v{latest}", current=current, latest=latest,
                          from_version=current, to_version=latest)
    started["started_at"] = int(rt.now())
    record(rt, state, started, dry_run=False)
    save_state(rt, state)
    write_log(rt, "panel", started["message"])
    ok, message, reloaded = rt.upgrade_panel()
    if ok and reloaded:
        return started  # 状态保持 started，由新面板启动时改成最终结果并发通知
    if ok:
        return make_result("panel", "up_to_date", message.splitlines()[0], current=current, latest=latest)
    return make_result("panel", "failed", message, current=current, latest=latest, from_version=current, to_version=latest)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def run_updates(rt, dry_run=False, only=None):
    settings = rt.panel.auto_update_settings()
    state = load_state(rt)
    reports = []
    stop_after_core = False

    def handle(report):
        if report is None:
            return
        reports.append(report)
        record(rt, state, report, dry_run)
        save_state(rt, state)
        prefix = "[检查] " if dry_run else ""
        label = rt.panel.AUTO_UPDATE_RESULT_LABELS.get(report["result"], report["result"])
        line = write_log(rt, report["item"], f"{prefix}{label}：{report['message']}")
        rt.echo(line)
        if not dry_run:
            notify_result(rt, report)

    state["last_run"] = int(rt.now())
    state["last_run_mode"] = "dry-run" if dry_run else "update"
    save_state(rt, state)
    write_log(rt, "run", "开始" + ("检查（不安装）" if dry_run else "自动更新") + (f"，只处理 {only}" if only else ""))

    for item in ("ui", "core", "panel"):
        if only and item != only:
            continue
        try:
            if item == "ui":
                handle(update_ui(rt, settings, state, dry_run))
            elif item == "core":
                report = update_core(rt, settings, dry_run)
                handle(report)
                stop_after_core = report["result"] == "rolled_back"
            elif stop_after_core:
                handle(make_result("panel", "skipped", "本次内核更新已回滚，面板升级推迟到下次", current=rt.panel.PANEL_VERSION))
            elif dry_run:
                handle(check_panel(rt, settings))
            else:
                handle(run_panel_upgrade(rt, state, settings))
        except Exception as e:
            handle(make_result(item, "failed", f"出错：{e}"))
    state["last_finished"] = int(rt.now())
    save_state(rt, state)
    success = all(r["result"] not in ("failed", "rolled_back") for r in reports)
    return {"success": success, "dry_run": dry_run, "items": reports, "finished_at": state["last_finished"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mihomo 自动更新（面板 UI / 内核 / 管理面板）")
    parser.add_argument("--dry-run", action="store_true", help="只检查并报告，不安装")
    parser.add_argument("--only", choices=("core", "ui", "panel"), help="只处理其中一项")
    parser.add_argument("--json", action="store_true", help="最后一行输出 JSON 报告（面板用）")
    parser.add_argument("--manual", action="store_true", help="手动触发：即使 .env 关闭了自动更新也执行")
    args = parser.parse_args(argv)

    panel = load_panel()
    rt = Runtime(panel)
    if args.json:
        rt.echo = lambda *_: None

    def finish(report, code):
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        return code

    if not args.manual and not panel.auto_update_settings()["enabled"]:
        write_log(rt, "run", "自动更新已关闭（AUTO_UPDATE_ENABLED=false），退出")
        return finish({"success": True, "dry_run": args.dry_run, "items": [], "message": "自动更新已关闭"}, 0)
    lock = panel.acquire_auto_update_lock(rt.lock_path)
    if lock is None:
        message = "另一个自动更新正在运行，本次退出"
        if not args.dry_run:
            write_log(rt, "run", message)
        print(message, file=sys.stderr)
        return finish({"success": False, "dry_run": args.dry_run, "items": [], "message": message}, EXIT_BUSY)
    try:
        report = run_updates(rt, dry_run=args.dry_run, only=args.only)
    finally:
        panel.release_auto_update_lock(lock)
    return finish(report, 0 if report["success"] else 1)


if __name__ == "__main__":
    sys.exit(main())
