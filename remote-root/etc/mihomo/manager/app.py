from flask import Flask, render_template, request, jsonify, Response, redirect, session
from functools import wraps
from datetime import timedelta
from collections import deque
import subprocess
import base64
import os
import re
import secrets
import shlex
import glob
import json
import shutil
import tempfile
import time
import zipfile
from urllib import request as urlrequest
from urllib.parse import quote

MIHOMO_DIR = "/etc/mihomo"
SCRIPT_DIR = "/etc/mihomo/scripts"
ENV_FILE = f"{MIHOMO_DIR}/.env"
CONFIG_FILE = f"{MIHOMO_DIR}/config.yaml"
LOG_FILE = "/var/log/mihomo.log"
BACKUP_DIR = f"{MIHOMO_DIR}/backup"
MANAGER_DIR = f"{MIHOMO_DIR}/manager"
PANEL_VERSION = "0.1.21"
DEFAULT_PANEL_REPO_URL = "https://github.com/anxiaoyang666/mihomo.git"
DEFAULT_PANEL_BRANCH = "main"
PANEL_BACKUP_KEEP_COUNT = 3
PANEL_UPGRADE_EXCLUDES = ("/etc/mihomo/.env", "/etc/mihomo/config.yaml", "/etc/mihomo/ui")
SYNCABLE_RULE_IDS = {"force-cn", "force-nocn"}
RULE_SYNC_ACTIONS = {"force-cn": "DIRECT", "force-nocn": "PROXY"}
RULE_SYNC_BEGIN = "# MOSCTL_MIHOMO_RULE_SYNC_BEGIN"
RULE_SYNC_END = "# MOSCTL_MIHOMO_RULE_SYNC_END"
FAKE_IP_FILTER_BEGIN = "# MOSCTL_MIHOMO_FAKE_IP_FILTER_BEGIN"
FAKE_IP_FILTER_END = "# MOSCTL_MIHOMO_FAKE_IP_FILTER_END"

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=365)

def run_args(args, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)

def is_service_active(service):
    try:
        result = subprocess.run(["systemctl", "is-active", service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False

def read_recent_log_lines(path, limit=100):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(deque(f, maxlen=limit))
    except Exception as e:
        return str(e)

def config_value(key):
    if not os.path.exists(CONFIG_FILE):
        return ""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                match = re.match(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", line)
                if match:
                    return match.group(1).strip().strip('"').strip("'")
    except:
        pass
    return ""

def read_env():
    env_data = {}
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    parsed = parse_env_line(line)
                    if parsed:
                        env_data[parsed[0]] = parsed[1]
        except: pass
    return env_data

def parse_env_line(line):
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in stripped:
        return None
    try:
        parts = shlex.split(stripped, comments=True, posix=True)
    except ValueError:
        parts = [stripped]
    if not parts or '=' not in parts[0]:
        return None
    key, value = parts[0].split('=', 1)
    key = key.strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        return None
    return key, value

def env_value_for_shell(value):
    normalized = str(value if value is not None else '')
    normalized = normalized.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n')
    return shlex.quote(normalized)

def env_line(key, value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        raise ValueError(f"Invalid env key: {key}")
    return f'{key}={env_value_for_shell(value)}\n'

def write_env(updates):
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        keys = set()
        for line in lines:
            parsed = parse_env_line(line)
            if parsed:
                k = parsed[0]
                if k in updates:
                    f.write(env_line(k, updates[k]))
                    keys.add(k)
                else:
                    f.write(line)
            else:
                f.write(line)
        for k, v in updates.items():
            if k not in keys:
                f.write(env_line(k, v))

def ensure_session_secret():
    env = read_env()
    secret = os.environ.get('WEB_SESSION_SECRET') or env.get('WEB_SESSION_SECRET')
    if not secret or secret == "mihomo-manager-secret":
        secret = secrets.token_urlsafe(48)
        write_env({"WEB_SESSION_SECRET": secret})
    app.secret_key = secret

ensure_session_secret()

def check_creds(username, password):
    env = read_env()
    valid_user = os.environ.get('WEB_USER') or env.get('WEB_USER', 'admin')
    valid_pass = os.environ.get('WEB_SECRET') or env.get('WEB_SECRET', 'admin')
    return username == valid_user and password == valid_pass

def is_valid_web_username(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", str(value or "")))

def update_cron(job_id, schedule, command, enabled):
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current_cron = res.stdout.strip().split('\n') if res.stdout else []
        new_cron = []
        for line in current_cron:
            if job_id not in line and line.strip() != "":
                new_cron.append(line)
        if enabled:
            new_cron.append(f"{schedule} {command} {job_id}")
        cron_str = "\n".join(new_cron) + "\n"
        subprocess.run(["crontab", "-"], input=cron_str, capture_output=True, text=True)
    except: pass

def parse_daily_time(value, default_hour, default_minute=0):
    match = re.match(r'^(\d{2}):(\d{2})$', str(value or ''))
    if not match:
        return default_hour, default_minute
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return default_hour, default_minute

def is_safe_cron(expr):
    return bool(re.match(r'^[\d*/, -]+\s+[\d*/, -]+\s+[\d*/, -]+\s+[\d*/, -]+\s+[\d*/, -]+$', str(expr or '').strip()))

def cron_to_mode(expr, default_time):
    parts = str(expr or '').strip().split()
    if len(parts) == 5:
        minute, hour, day, month, weekday = parts
        if day == '*' and month == '*' and weekday == '*':
            if re.fullmatch(r'\d+', minute) and re.fullmatch(r'\d+', hour):
                return {"mode": "daily", "time": f"{int(hour):02d}:{int(minute):02d}"}
            if re.fullmatch(r'\d+', minute) and hour in ('*/6', '0,6,12,18'):
                return {"mode": "every6h", "time": f"00:{int(minute):02d}"}
            if re.fullmatch(r'\d+', minute) and hour in ('*/12', '0,12'):
                return {"mode": "every12h", "time": f"00:{int(minute):02d}"}
    return {"mode": "advanced", "time": default_time}

def build_schedule(mode, time_value, advanced_value, default_cron):
    mode = mode if mode in ("daily", "every6h", "every12h", "advanced") else "daily"
    if mode == "advanced":
        advanced = str(advanced_value or default_cron).strip()
        return advanced if is_safe_cron(advanced) else default_cron
    hour, minute = parse_daily_time(time_value, 5)
    if mode == "daily":
        return f"{minute} {hour} * * *"
    if mode == "every6h":
        return f"{minute} */6 * * *"
    if mode == "every12h":
        return f"{minute} */12 * * *"
    return default_cron

def validate_config(path):
    checker = "/usr/bin/mihomo-core"
    if not os.path.exists(checker):
        return True, "未找到 mihomo-core，已跳过配置校验。"
    result = subprocess.run([checker, "-t", "-d", MIHOMO_DIR, "-f", path], capture_output=True, text=True, timeout=30)
    return result.returncode == 0, result.stdout + result.stderr

def is_true(val):
    return str(val).lower() == 'true'

def is_safe_text(value, max_len=50000):
    return isinstance(value, str) and len(value.encode("utf-8")) <= max_len and "\x00" not in value

def parse_peers(value):
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[\n,|]+", str(value or ""))
    peers = []
    for item in parts:
        peer = str(item or "").strip().rstrip("/")
        if not peer:
            continue
        if not peer.startswith(("http://", "https://")):
            peer = "http://" + peer
        peers.append(peer)
    return list(dict.fromkeys(peers))

def normalize_rule_domains(content):
    domains = []
    for line in str(content or "").replace("|", "\n").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        item = re.sub(r"^(DOMAIN-SUFFIX,|DOMAIN,|full:)", "", item, flags=re.IGNORECASE).strip()
        item = item.lstrip(".")
        if re.fullmatch(r"[A-Za-z0-9*_.-]+\.[A-Za-z0-9_.-]+", item):
            domains.append(item.lower())
    return list(dict.fromkeys(domains))

def read_sync_settings():
    env = read_env()
    peers = parse_peers(env.get("RULE_SYNC_PEERS", ""))
    return {
        "enabled": is_true(env.get("RULE_SYNC_ENABLED")),
        "token": env.get("RULE_SYNC_TOKEN", ""),
        "peers": peers,
        "peers_text": "\n".join(peers),
        "syncable_rules": sorted(SYNCABLE_RULE_IDS),
    }

def write_sync_settings(data):
    peers = parse_peers(data.get("peers_text") or data.get("peers") or "")
    token = str(data.get("token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
    if not is_safe_text(token, 200) or "\n" in token or "\r" in token:
        return False, "同步密钥不合法"
    write_env(
        {
            "RULE_SYNC_ENABLED": str(is_true(data.get("enabled"))).lower(),
            "RULE_SYNC_TOKEN": token,
            "RULE_SYNC_PEERS": "|".join(peers),
        }
    )
    return True, "规则同步设置已保存"

def read_config_text():
    if not os.path.exists(CONFIG_FILE):
        return "rules:\n"
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return f.read()

def write_config_text(text):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.copy2(CONFIG_FILE, f"{BACKUP_DIR}/config.before-rule-sync.{time.strftime('%Y%m%d%H%M%S')}.yaml")
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(text)

def find_yaml_top_level_key(lines, key):
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        if pattern.match(line.rstrip("\n")):
            return index
    return -1

def line_indent(line):
    return re.match(r"^\s*", line).group(0)

def is_yaml_section_at_or_above(line, indent_len):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return len(line_indent(line)) <= indent_len and re.match(r"^[^#:\s][^#]*:\s*(?:#.*)?$", line[indent_len:]) is not None

def proxy_policy_name(text):
    for preferred in ("♻️ 自动选择", "🚀 默认代理", "Final", "GLOBAL"):
        if preferred in text:
            return preferred
    match = re.search(r"(?m)^\s*-\s*name:\s*['\"]?([^'\"\n]+)['\"]?\s*$", text)
    return match.group(1).strip() if match else "PROXY"

def read_mihomo_sync_rules():
    text = read_config_text()
    rules = {"force-cn": [], "force-nocn": []}
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == RULE_SYNC_BEGIN:
            in_block = True
            continue
        if stripped == RULE_SYNC_END:
            in_block = False
            continue
        if not in_block:
            continue
        if stripped.startswith("- DOMAIN-SUFFIX,"):
            parts = stripped[2:].split(",", 2)
            if len(parts) == 3:
                target = "force-cn" if parts[2] == "DIRECT" else "force-nocn"
                rules[target].append(parts[1])
    return {key: "\n".join(value) + ("\n" if value else "") for key, value in rules.items()}

def build_mihomo_sync_rule_lines(rule_contents, proxy_policy):
    block = [f"{RULE_SYNC_BEGIN}\n"]
    for domain in normalize_rule_domains(rule_contents.get("force-cn", "")):
        block.append(f"- DOMAIN-SUFFIX,{domain},DIRECT\n")
    for domain in normalize_rule_domains(rule_contents.get("force-nocn", "")):
        block.append(f"- DOMAIN-SUFFIX,{domain},{proxy_policy}\n")
    block.append(f"{RULE_SYNC_END}\n")
    return block

def build_fake_ip_filter_lines(rule_contents, item_indent):
    block = [f"{item_indent}{FAKE_IP_FILTER_BEGIN}\n"]
    for domain in normalize_rule_domains(rule_contents.get("force-cn", "")):
        block.append(f"{item_indent}- +.{domain}\n")
    block.append(f"{item_indent}{FAKE_IP_FILTER_END}\n")
    return block

def remove_fake_ip_filter_block(lines):
    start = next((idx for idx, line in enumerate(lines) if line.strip() == FAKE_IP_FILTER_BEGIN), -1)
    if start < 0:
        return lines
    end = next((idx for idx in range(start + 1, len(lines)) if lines[idx].strip() == FAKE_IP_FILTER_END), start)
    return lines[:start] + lines[end + 1:]

def find_nested_key(lines, section_index, key):
    parent_indent = len(line_indent(lines[section_index]))
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(?:#.*)?$")
    for index in range(section_index + 1, len(lines)):
        if is_yaml_section_at_or_above(lines[index], parent_indent):
            break
        if pattern.match(lines[index].rstrip("\n")):
            return index
    return -1

def section_end_index(lines, section_index):
    parent_indent = len(line_indent(lines[section_index]))
    for index in range(section_index + 1, len(lines)):
        if is_yaml_section_at_or_above(lines[index], parent_indent):
            return index
    return len(lines)

def list_item_indent_after_key(lines, key_index):
    key_indent = line_indent(lines[key_index])
    key_indent_len = len(key_indent)
    for index in range(key_index + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if is_yaml_section_at_or_above(line, key_indent_len):
            break
        if re.match(r"^\s*-\s+", line):
            return line_indent(line)
    return key_indent + "  "

def update_fake_ip_filter_block(lines, rule_contents):
    lines = remove_fake_ip_filter_block(lines)
    dns_index = find_yaml_top_level_key(lines, "dns")
    if dns_index < 0:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("dns:\n")
        lines.append("  fake-ip-filter:\n")
        lines.extend(build_fake_ip_filter_lines(rule_contents, "    "))
        return lines

    fake_filter_index = find_nested_key(lines, dns_index, "fake-ip-filter")
    if fake_filter_index < 0:
        dns_indent = line_indent(lines[dns_index])
        child_indent = dns_indent + "  "
        item_indent = child_indent + "  "
        insert_at = section_end_index(lines, dns_index)
        lines[insert_at:insert_at] = [f"{child_indent}fake-ip-filter:\n"] + build_fake_ip_filter_lines(rule_contents, item_indent)
        return lines

    item_indent = list_item_indent_after_key(lines, fake_filter_index)
    lines[fake_filter_index + 1:fake_filter_index + 1] = build_fake_ip_filter_lines(rule_contents, item_indent)
    return lines

def update_mihomo_sync_block(rule_contents):
    text = read_config_text()
    lines = text.splitlines(True)
    if not lines:
        lines = ["rules:\n"]
    proxy_policy = proxy_policy_name(text)
    block = build_mihomo_sync_rule_lines(rule_contents, proxy_policy)

    start = next((idx for idx, line in enumerate(lines) if line.strip() == RULE_SYNC_BEGIN), -1)
    if start >= 0:
        end = next((idx for idx in range(start + 1, len(lines)) if lines[idx].strip() == RULE_SYNC_END), start)
        lines[start:end + 1] = block
    else:
        rules_index = find_yaml_top_level_key(lines, "rules")
        if rules_index < 0:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append("rules:\n")
            rules_index = len(lines) - 1
        lines[rules_index + 1:rules_index + 1] = block
    lines = update_fake_ip_filter_block(lines, rule_contents)
    new_text = "".join(lines)
    tmp_file = f"{CONFIG_FILE}.rulesync"
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(new_text)
    ok, message = validate_config(tmp_file)
    if not ok:
        try:
            os.remove(tmp_file)
        except OSError:
            pass
        return False, "规则写入后配置校验失败，已取消保存：\n" + message
    os.remove(tmp_file)
    write_config_text(new_text)
    return True, "规则已写入 mihomo 配置"

def save_rule_content(rule_id, content):
    if rule_id not in SYNCABLE_RULE_IDS:
        return False, "未知规则文件"
    if not is_safe_text(content):
        return False, "规则内容不合法或过大"
    current = read_mihomo_sync_rules()
    current[rule_id] = "\n".join(normalize_rule_domains(content))
    if current[rule_id]:
        current[rule_id] += "\n"
    return update_mihomo_sync_block(current)

def restart_mihomo():
    return run_args(["systemctl", "restart", "mihomo"], timeout=60)

def broadcast_rule(rule_id, content):
    if rule_id not in SYNCABLE_RULE_IDS:
        return ""
    settings = read_sync_settings()
    if not settings["enabled"]:
        return "规则同步未启用。"
    if not settings["peers"]:
        return "规则同步已启用，但没有配置其他节点。"
    if not settings["token"]:
        return "规则同步已启用，但缺少同步密钥。"

    payload = json.dumps(
        {
            "token": settings["token"],
            "rules": {rule_id: content},
            "source": request.host_url.rstrip("/"),
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Mosdns-Sync-Token": settings["token"]}
    results = []
    for peer in settings["peers"]:
        url = peer.rstrip("/") + "/api/rule-sync"
        try:
            req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if body.get("success"):
                results.append(f"{peer}: 成功")
            else:
                results.append(f"{peer}: 失败 - {body.get('message', '未知错误')}")
        except Exception as exc:
            results.append(f"{peer}: 失败 - {exc}")
    return "同步结果：\n" + "\n".join(results)

def apply_synced_rules(rules):
    if not isinstance(rules, dict):
        return False, "同步内容不合法"
    current = read_mihomo_sync_rules()
    applied = []
    for rule_id, content in rules.items():
        if rule_id not in SYNCABLE_RULE_IDS:
            continue
        if not is_safe_text(content):
            return False, "规则内容不合法或过大"
        current[rule_id] = "\n".join(normalize_rule_domains(content))
        if current[rule_id]:
            current[rule_id] += "\n"
        applied.append(rule_id)
    if not applied:
        return False, "没有可同步的规则"
    ok, message = update_mihomo_sync_block(current)
    if not ok:
        return False, message
    restarted, restart_message = restart_mihomo()
    if not restarted:
        return False, "规则已写入，但 mihomo 重启失败：\n" + restart_message
    return True, "已同步规则：" + ", ".join(applied)

def test_sync_peers(data):
    peers = parse_peers(data.get("peers_text") or data.get("peers") or "")
    token = str(data.get("token") or "").strip()
    if not peers:
        return False, "请先填写其他 mosctl / mihomo 面板地址", []
    if not token:
        return False, "请先填写同步密钥", []
    payload = json.dumps({"token": token, "rules": {}, "source": request.host_url.rstrip("/")}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Mosdns-Sync-Token": token}
    results = []
    for peer in peers:
        url = peer.rstrip("/") + "/api/rule-sync"
        try:
            req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=8) as resp:
                body_text = resp.read().decode("utf-8", "replace")
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                body = {}
            message = body.get("message") or "接口可访问，密钥已通过"
            if message == "没有可同步的规则":
                message = "接口可访问，密钥已通过"
            results.append({"peer": peer, "success": True, "message": message})
        except Exception as exc:
            results.append({"peer": peer, "success": False, "message": str(exc)})
    return all(item["success"] for item in results), "连通性测试完成", results

def mihomo_controller_settings():
    env = read_env()
    controller = (
        os.environ.get("MIHOMO_CONTROLLER")
        or env.get("MIHOMO_CONTROLLER")
        or config_value("external-controller")
        or "127.0.0.1:9090"
    )
    controller = str(controller).strip().strip('"').strip("'")
    if controller.startswith(":"):
        controller = "127.0.0.1" + controller
    if "://" not in controller:
        controller = "http://" + controller
    controller = controller.replace("0.0.0.0", "127.0.0.1").replace("[::]", "127.0.0.1")
    secret = os.environ.get("MIHOMO_API_SECRET") or env.get("MIHOMO_API_SECRET") or config_value("secret")
    return {"base_url": controller.rstrip("/"), "secret": secret}

def mihomo_api_get(path, timeout=2):
    settings = mihomo_controller_settings()
    url = settings["base_url"] + path
    headers = {"User-Agent": "mihomo-web-manager"}
    if settings.get("secret"):
        headers["Authorization"] = "Bearer " + settings["secret"]
    try:
        req = urlrequest.Request(url, headers=headers)
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, {"error": str(e), "url": url}

def first_number(value):
    try:
        return int(value or 0)
    except:
        return 0

def latest_delay(proxy):
    history = proxy.get("history") if isinstance(proxy, dict) else None
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        delay = item.get("delay")
        if isinstance(delay, (int, float)) and delay >= 0:
            return delay
    return None

def proxy_group_summary(proxies):
    items = proxies.get("proxies") if isinstance(proxies, dict) else {}
    if not isinstance(items, dict):
        return []
    groups = []
    for name, proxy in items.items():
        if not isinstance(proxy, dict) or "all" not in proxy:
            continue
        groups.append({
            "name": name,
            "type": proxy.get("type", "Group"),
            "now": proxy.get("now", ""),
            "count": len(proxy.get("all") or []),
            "delay": latest_delay(proxy),
        })
    return groups[:8]

def log_level_summary():
    levels = {"error": 0, "warn": 0, "info": 0, "debug": 0}
    if not os.path.exists(LOG_FILE):
        return levels
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
        for line in lines:
            match = re.search(r"level=([a-zA-Z]+)", line)
            if not match:
                continue
            level = match.group(1).lower()
            if level in ("warning", "warn"):
                levels["warn"] += 1
            elif level in levels:
                levels[level] += 1
    except:
        pass
    return levels

def subscription_count(env):
    raw = [env.get("SUB_URL_RAW", "")]
    airport = str(env.get("SUB_URL_AIRPORT", "")).replace("\\n", "\n").splitlines()
    return len([item for item in raw + airport if item.strip()])

def collect_overview():
    env = read_env()
    running = is_service_active("mihomo")
    controller = mihomo_controller_settings()
    connections_ok, connections = mihomo_api_get("/connections")
    version_ok, version = mihomo_api_get("/version")
    proxies_ok, proxies = mihomo_api_get("/proxies")
    connection_list = connections.get("connections") if isinstance(connections.get("connections"), list) else []
    proxy_groups = proxy_group_summary(proxies) if proxies_ok else []
    return {
        "running": running,
        "controller": {
            "base_url": controller["base_url"],
            "reachable": bool(connections_ok or version_ok or proxies_ok),
            "error": "" if (connections_ok or version_ok or proxies_ok) else connections.get("error", "controller unreachable"),
        },
        "panel_version": PANEL_VERSION,
        "core_version": version.get("version", "") if version_ok else "",
        "connections_count": len(connection_list),
        "download_total": first_number(connections.get("downloadTotal")),
        "upload_total": first_number(connections.get("uploadTotal")),
        "memory": first_number(connections.get("memory")),
        "proxy_groups": proxy_groups,
        "log_levels": log_level_summary(),
        "settings": {
            "config_mode": env.get("CONFIG_MODE", "airport"),
            "subscription_count": subscription_count(env),
            "cron_sub_enabled": env.get("CRON_SUB_ENABLED") == "true",
            "cron_geo_enabled": env.get("CRON_GEO_ENABLED") == "true",
            "notify_api": env.get("NOTIFY_API") == "true",
            "local_cidr": env.get("LOCAL_CIDR", ""),
        },
        "updated_at": int(time.time()),
    }

def panel_repo_settings():
    env = read_env()
    return {
        "repo_url": env.get("MIHOMO_PANEL_REPO_URL") or DEFAULT_PANEL_REPO_URL,
        "branch": env.get("MIHOMO_PANEL_BRANCH") or DEFAULT_PANEL_BRANCH,
    }

def github_repo_parts(repo_url):
    repo = str(repo_url or "").strip()
    repo = repo[:-4] if repo.endswith(".git") else repo
    match = re.match(r"^https://github\.com/([^/\s]+)/([^/\s]+)$", repo)
    if not match:
        return None
    return match.groups()

def github_archive_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    return f"https://github.com/{owner}/{name}/archive/refs/heads/{quote(branch, safe='/')}.zip"

def github_raw_app_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    return f"https://raw.githubusercontent.com/{owner}/{name}/{quote(branch, safe='/')}/remote-root/etc/mihomo/manager/app.py"

def github_contents_app_url(repo_url, branch):
    parts = github_repo_parts(repo_url)
    if not parts:
        return ""
    owner, name = parts
    path = "remote-root/etc/mihomo/manager/app.py"
    return f"https://api.github.com/repos/{owner}/{name}/contents/{path}?ref={quote(branch, safe='/')}"

def parse_github_contents_text(text):
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        return ""
    if data.get("encoding") != "base64" or not data.get("content"):
        return ""
    payload = str(data.get("content") or "").replace("\n", "")
    return base64.b64decode(payload).decode("utf-8", "replace")

def read_url_text(urls, timeout=15):
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mihomo-web-manager"})
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                return True, resp.read().decode("utf-8", "replace"), url
        except Exception as e:
            last_error = str(e)
    return False, last_error, ""

def download_file(urls, output, timeout=30):
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mihomo-web-manager"})
            with urlrequest.urlopen(req, timeout=timeout) as resp, open(output, "wb") as f:
                shutil.copyfileobj(resp, f)
            return True, url
        except Exception as e:
            last_error = str(e)
    return False, last_error

def safe_extract_zip(archive, destination):
    dest_root = os.path.abspath(destination)
    for member in archive.infolist():
        target = os.path.abspath(os.path.join(destination, member.filename))
        if target != dest_root and not target.startswith(dest_root + os.sep):
            raise ValueError("Unsafe path in zip archive: " + member.filename)
        archive.extract(member, destination)

def panel_version_tuple(value):
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())

def parse_panel_version(text):
    match = re.search(r'(?m)^PANEL_VERSION\s*=\s*["\']([^"\']+)["\']\s*$', text or "")
    return match.group(1).strip() if match else ""

def remote_panel_version(settings=None):
    settings = settings or panel_repo_settings()
    raw_url = github_raw_app_url(settings["repo_url"], settings["branch"])
    contents_url = github_contents_app_url(settings["repo_url"], settings["branch"])
    if not raw_url or not contents_url:
        return {"success": False, "latest_version": "", "source": "", "message": "当前只支持 GitHub 仓库地址。"}
    ok, text, source = read_url_text([contents_url, f"https://gh-proxy.com/{raw_url}", raw_url], timeout=15)
    if ok:
        if source == contents_url:
            text = parse_github_contents_text(text)
        version = parse_panel_version(text)
        if version:
            return {"success": True, "latest_version": version, "source": source, "message": ""}
        return {"success": False, "latest_version": "", "source": source, "message": "远端 app.py 没有声明 PANEL_VERSION。"}
    return {"success": False, "latest_version": "", "source": "", "message": text}

def panel_upgrade_state():
    settings = panel_repo_settings()
    remote = remote_panel_version(settings)
    current_tuple = panel_version_tuple(PANEL_VERSION)
    latest_tuple = panel_version_tuple(remote.get("latest_version"))
    update_available = bool(remote.get("success") and current_tuple and latest_tuple and latest_tuple > current_tuple)
    return {
        **settings,
        "archive_url": github_archive_url(settings["repo_url"], settings["branch"]),
        "supported": bool(github_archive_url(settings["repo_url"], settings["branch"])),
        "current_version": PANEL_VERSION,
        "latest_version": remote.get("latest_version", ""),
        "update_available": update_available,
        "check_success": remote.get("success", False),
        "source": remote.get("source", ""),
        "message": remote.get("message", ""),
    }

def panel_managed_targets():
    return [
        (MANAGER_DIR, "etc/mihomo/manager", "dir", 0o755),
        (SCRIPT_DIR, "etc/mihomo/scripts", "dir", 0o755),
        (f"{MIHOMO_DIR}/templates", "etc/mihomo/templates", "dir", 0o755),
        ("/usr/bin/mihomo", "usr/bin/mihomo", "file", 0o755),
        (f"{MIHOMO_DIR}/config.example.yaml", "etc/mihomo/config.example.yaml", "file", 0o644),
        ("/etc/systemd/system/mihomo.service", "etc/systemd/system/mihomo.service", "file", 0o644),
        ("/etc/systemd/system/mihomo-manager.service", "etc/systemd/system/mihomo-manager.service", "file", 0o644),
        ("/etc/systemd/system/force-ip-forward.service", "etc/systemd/system/force-ip-forward.service", "file", 0o644),
    ]

def download_panel_source(tmpdir):
    settings = panel_repo_settings()
    archive_url = github_archive_url(settings["repo_url"], settings["branch"])
    if not archive_url:
        return False, "当前只支持 GitHub 仓库地址。", None, settings
    zip_path = os.path.join(tmpdir, "mihomo-panel.zip")
    ok, source = download_file([f"https://gh-proxy.com/{archive_url}", archive_url], zip_path)
    if not ok:
        return False, "下载升级包失败：\n" + source, None, settings
    try:
        with zipfile.ZipFile(zip_path) as archive:
            safe_extract_zip(archive, tmpdir)
    except zipfile.BadZipFile:
        return False, "下载到的文件不是有效的 zip 压缩包。", None, settings
    except ValueError as e:
        return False, str(e), None, settings

    for root, dirs, _ in os.walk(tmpdir):
        if "remote-root" not in dirs:
            continue
        source_root = os.path.join(root, "remote-root")
        app_path = os.path.join(source_root, "etc/mihomo/manager/app.py")
        cli_path = os.path.join(source_root, "usr/bin/mihomo")
        if not os.path.exists(app_path) or not os.path.exists(cli_path):
            continue
        ok, message = run_args(["python3", "-m", "py_compile", app_path], timeout=20)
        if not ok:
            return False, "新版 app.py 校验失败：\n" + message, None, settings
        with open(app_path, "r", encoding="utf-8") as f:
            settings["remote_version"] = parse_panel_version(f.read())
        return True, source, source_root, settings
    return False, "升级包里没有找到有效的 remote-root 目录。", None, settings

def backup_panel_targets():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup_root = f"{BACKUP_DIR}/mihomo-panel.{stamp}"
    os.makedirs(backup_root, exist_ok=True)
    manifest = []
    for target, _, kind, _ in panel_managed_targets():
        backup_path = os.path.join(backup_root, target.lstrip("/"))
        existed = os.path.exists(target)
        manifest.append({"target": target, "kind": kind, "existed": existed})
        if not existed:
            continue
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        if os.path.isdir(target):
            shutil.copytree(target, backup_path)
        else:
            shutil.copy2(target, backup_path)
    with open(os.path.join(backup_root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return backup_root

def restore_panel_backup(backup_root):
    manifest_path = os.path.join(backup_root, "manifest.json")
    if not os.path.exists(manifest_path):
        return
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    for item in manifest:
        target = item["target"]
        backup_path = os.path.join(backup_root, target.lstrip("/"))
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target):
            os.remove(target)
        if not item.get("existed"):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if item.get("kind") == "dir":
            shutil.copytree(backup_path, target)
        else:
            shutil.copy2(backup_path, target)

def cleanup_panel_backups():
    backups = [path for path in glob.glob(f"{BACKUP_DIR}/mihomo-panel.*") if os.path.isdir(path)]
    backups.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for path in backups[PANEL_BACKUP_KEEP_COUNT:]:
        shutil.rmtree(path, ignore_errors=True)

def install_panel_payload(source_root):
    for target, relative, kind, mode in panel_managed_targets():
        if target in PANEL_UPGRADE_EXCLUDES:
            continue
        source = os.path.join(source_root, relative)
        if not os.path.exists(source):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if kind == "dir":
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
            os.chmod(target, mode)
    run_args(["systemctl", "daemon-reload"], timeout=30)

def schedule_panel_restart():
    subprocess.Popen(
        ["sh", "-c", "sleep 1; systemctl restart mihomo-manager"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

def upgrade_panel():
    with tempfile.TemporaryDirectory() as tmpdir:
        ok, source, source_root, settings = download_panel_source(tmpdir)
        if not ok:
            return False, source, False
        remote_version = settings.get("remote_version", "")
        current_tuple = panel_version_tuple(PANEL_VERSION)
        remote_tuple = panel_version_tuple(remote_version)
        if not remote_tuple:
            return False, "远端面板没有声明 PANEL_VERSION，已取消升级以避免降级。", False
        if current_tuple and remote_tuple <= current_tuple:
            return True, f"当前面板已经是最新版本。\n当前版本：v{PANEL_VERSION}\n远端版本：v{remote_version}", False
        backup_root = backup_panel_targets()
        try:
            install_panel_payload(source_root)
            cleanup_panel_backups()
        except Exception as e:
            restore_panel_backup(backup_root)
            run_args(["systemctl", "daemon-reload"], timeout=30)
            return False, "面板升级失败，已自动回滚：\n" + str(e), False
    schedule_panel_restart()
    return True, (
        "Mihomo 面板升级完成，Web 服务将在 1 秒后重启。\n"
        f"升级源：{source}\n"
        f"仓库：{settings['repo_url']}\n"
        f"分支：{settings['branch']}\n"
        f"旧版本：v{PANEL_VERSION}\n"
        f"新版本：v{remote_version}\n"
        f"备份位置：{backup_root}"
    ), True

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api'): return jsonify({"error": "Unauthorized"}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if check_creds(request.form.get('username'), request.form.get('password')):
            session['logged_in'] = True
            session.permanent = True
            return redirect('/')
        return render_template('login.html', error="用户名或密码错误")
    
    if session.get('logged_in'):
        return redirect('/')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/')
def index():
    if session.get('logged_in'):
        return render_template('index.html')
    return redirect('/login')

@app.route('/api/status')
@login_required
def get_status():
    return jsonify({
        "running": is_service_active("mihomo"),
        "panel_version": PANEL_VERSION,
    })

@app.route('/api/overview')
@login_required
def api_overview():
    return jsonify(collect_overview())

@app.route('/api/panel-upgrade-source')
@login_required
def api_panel_upgrade_source():
    return jsonify(panel_upgrade_state())

@app.route('/api/control', methods=['POST'])
@login_required
def control_service():
    action = request.json.get('action')
    if action == 'upgrade_panel':
        ok, message, should_reload = upgrade_panel()
        return jsonify({"success": ok, "message": message, "reload_after": 5 if should_reload else 0})
    control_actions = {
        'start': ['systemctl', 'start', 'mihomo'],
        'stop': ['systemctl', 'stop', 'mihomo'],
        'restart': ['systemctl', 'restart', 'mihomo'],
        'fix_logs': ['systemctl', 'restart', 'mihomo'],
        'update_sub': ['bash', f'{SCRIPT_DIR}/update_subscription.sh'],
        'update_geo': ['bash', f'{SCRIPT_DIR}/update_geo.sh'],
        'net_init': ['bash', f'{SCRIPT_DIR}/gateway_init.sh'],
        'test_notify': ['bash', f'{SCRIPT_DIR}/notify.sh', '测试', 'Web端测试消息']
    }
    if action in control_actions:
        s, m = run_args(control_actions[action], timeout=180)
        return jsonify({"success": s, "message": m})
    return jsonify({"success": False, "message": "未知指令"})

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def handle_config():
    if request.method == 'GET':
        c = ""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE,'r', encoding='utf-8') as f:
                    c = f.read()
            except:
                pass
        return jsonify({"content": c})
    if request.method == 'POST':
        try:
            content = request.json.get('content') or ''
            tmp_file = f"{CONFIG_FILE}.webcheck"
            with open(tmp_file, 'w', encoding='utf-8') as f:
                f.write(content)
            ok, message = validate_config(tmp_file)
            if not ok:
                try: os.remove(tmp_file)
                except: pass
                return jsonify({"success": False, "message": "配置校验失败，未保存：\n" + message})
            os.replace(tmp_file, CONFIG_FILE)
            return jsonify({"success": True, "message": "配置已保存"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

@app.route('/api/logs')
@login_required
def get_logs():
    if not os.path.exists(LOG_FILE): return jsonify({"logs": "日志未生成"})
    logs = read_recent_log_lines(LOG_FILE, 100)
    return jsonify({"logs": logs if logs else "暂无日志"})

@app.route('/api/account', methods=['POST'])
@login_required
def update_account_credentials():
    data = request.json or {}
    env = read_env()
    valid_pass = os.environ.get('WEB_SECRET') or env.get('WEB_SECRET', 'admin')
    current_password = str(data.get('current_password') or '')
    new_user = str(data.get('web_user') or '').strip()
    new_password = str(data.get('web_secret') or '')
    confirm_password = str(data.get('web_secret_confirm') or '')

    if current_password != valid_pass:
        return jsonify({"success": False, "message": "当前密码不正确。"})
    if not is_valid_web_username(new_user):
        return jsonify({"success": False, "message": "用户名只能使用 3-32 位字母、数字、下划线、点或短横线。"})

    updates = {"WEB_USER": new_user}
    if new_password:
        if len(new_password) < 6:
            return jsonify({"success": False, "message": "新密码至少需要 6 位。"})
        if new_password != confirm_password:
            return jsonify({"success": False, "message": "两次输入的新密码不一致。"})
        updates.update({"WEB_SECRET": new_password})

    write_env(updates)
    os.environ["WEB_USER"] = new_user
    if "WEB_SECRET" in updates:
        os.environ["WEB_SECRET"] = updates["WEB_SECRET"]
    session.clear()
    return jsonify({"success": True, "message": "账号已更新，请使用新凭据重新登录。", "reload_after": 1})

@app.route("/api/rule-sync-settings", methods=["GET", "POST"])
@login_required
def api_rule_sync_settings():
    if request.method == "GET":
        return jsonify(read_sync_settings())
    ok, message = write_sync_settings(request.json or {})
    return jsonify({"success": ok, "message": message, **read_sync_settings()})

@app.route("/api/rule-sync-test", methods=["POST"])
@login_required
def api_rule_sync_test():
    ok, message, results = test_sync_peers(request.json or {})
    return jsonify({"success": ok, "message": message, "results": results})

@app.route("/api/rule-sync", methods=["POST"])
def api_rule_sync():
    env = read_env()
    expected = env.get("RULE_SYNC_TOKEN", "")
    provided = request.headers.get("X-Mosdns-Sync-Token", "")
    data = request.json or {}
    if not provided:
        provided = str(data.get("token") or "")
    if not expected or not secrets.compare_digest(provided, expected):
        return jsonify({"success": False, "message": "同步密钥错误"}), 403
    ok, message = apply_synced_rules(data.get("rules"))
    return jsonify({"success": ok, "message": message})

@app.route("/api/rules/<rule_id>", methods=["GET", "POST"])
@login_required
def api_rules(rule_id):
    if rule_id not in SYNCABLE_RULE_IDS:
        return jsonify({"success": False, "message": "未知规则文件"}), 404
    labels = {"force-cn": "强制直连", "force-nocn": "强制代理"}
    summaries = {
        "force-cn": "这些域名会写入 mihomo 规则顶部并强制 DIRECT。",
        "force-nocn": "这些域名会写入 mihomo 规则顶部并强制走代理策略组。",
    }
    if request.method == "GET":
        return jsonify(
            {
                "id": rule_id,
                "label": labels[rule_id],
                "summary": summaries[rule_id],
                "format": "每行一个域名，例如 example.com",
                "examples": ["example.com", "full.example.com"],
                "content": read_mihomo_sync_rules().get(rule_id, ""),
            }
        )
    content = (request.json or {}).get("content", "")
    saved, save_message = save_rule_content(rule_id, content)
    if not saved:
        return jsonify({"success": False, "message": save_message})
    ok, message = restart_mihomo()
    if ok:
        sync_message = broadcast_rule(rule_id, content)
        if sync_message:
            message = "规则已保存并重启 mihomo\n\n" + sync_message
    return jsonify(
        {
            "success": ok,
            "message": message if ok else "规则已保存，但 mihomo 重启失败：\n" + message,
        }
    )

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def handle_settings():
    if request.method == 'GET':
        e = read_env()
        sub_url_airport = e.get('SUB_URL_AIRPORT', '').replace('\\n', '\n')
        sub_schedule = cron_to_mode(e.get('CRON_SUB_SCHED', '0 5 * * *'), '05:00')
        geo_schedule = cron_to_mode(e.get('CRON_GEO_SCHED', '0 4 * * *'), '04:00')
        
        return jsonify({
            "web_user": os.environ.get('WEB_USER') or e.get('WEB_USER', 'admin'),
            "web_port": e.get('WEB_PORT', '7838'),
            
            "config_mode": e.get('CONFIG_MODE', 'airport'),
            "sub_url_raw": e.get('SUB_URL_RAW', ''),
            "sub_url_airport": sub_url_airport,
            
            # 仅 Webhook
            "notify_api": e.get('NOTIFY_API') == 'true',
            "api_url": e.get('NOTIFY_API_URL', ''),
            "notify_api_url": e.get('NOTIFY_API_URL', ''),
            
            "local_cidr": e.get('LOCAL_CIDR', ''),
            "cron_sub_enabled": e.get('CRON_SUB_ENABLED') == 'true',
            "cron_sub_sched": e.get('CRON_SUB_SCHED', '0 5 * * *'), 
            "cron_sub_schedule": e.get('CRON_SUB_SCHED', '0 5 * * *'),
            "cron_sub_mode": e.get('CRON_SUB_MODE', sub_schedule['mode']),
            "cron_sub_time": e.get('CRON_SUB_TIME', sub_schedule['time']),
            "cron_geo_enabled": e.get('CRON_GEO_ENABLED') == 'true',
            "cron_geo_sched": e.get('CRON_GEO_SCHED', '0 4 * * *'),
            "cron_geo_schedule": e.get('CRON_GEO_SCHED', '0 4 * * *'),
            "cron_geo_mode": e.get('CRON_GEO_MODE', geo_schedule['mode']),
            "cron_geo_time": e.get('CRON_GEO_TIME', geo_schedule['time'])
        })

    if request.method == 'POST':
        d = request.json
        mode = d.get('config_mode', 'airport')
        
        raw_airport = d.get('sub_url_airport', '')
        if isinstance(raw_airport, list):
            raw_airport = "\n".join(raw_airport)
        escaped_airport = raw_airport.replace('\n', '\\n')

        api_url = d.get('api_url') or d.get('notify_api_url') or ''
        cron_sub_mode = d.get('cron_sub_mode', 'daily')
        cron_sub_time = d.get('cron_sub_time', '05:00')
        cron_sub = build_schedule(cron_sub_mode, cron_sub_time, d.get('cron_sub_sched') or d.get('cron_sub_schedule'), '0 5 * * *')
        cron_geo_mode = d.get('cron_geo_mode', 'daily')
        cron_geo_time = d.get('cron_geo_time', '04:00')
        cron_geo = build_schedule(cron_geo_mode, cron_geo_time, d.get('cron_geo_sched') or d.get('cron_geo_schedule'), '0 4 * * *')

        updates = {
            "CONFIG_MODE": mode,
            "SUB_URL_RAW": d.get('sub_url_raw', ''),
            "SUB_URL_AIRPORT": escaped_airport,
            
            # 仅更新 API 配置
            "NOTIFY_API": str(is_true(d.get('notify_api'))).lower(),
            "NOTIFY_API_URL": api_url,
            
            "LOCAL_CIDR": d.get('local_cidr', ''),
            
            "CRON_SUB_ENABLED": str(is_true(d.get('cron_sub_enabled'))).lower(),
            "CRON_SUB_SCHED": cron_sub,
            "CRON_SUB_MODE": cron_sub_mode,
            "CRON_SUB_TIME": cron_sub_time,
            
            "CRON_GEO_ENABLED": str(is_true(d.get('cron_geo_enabled'))).lower(),
            "CRON_GEO_SCHED": cron_geo,
            "CRON_GEO_MODE": cron_geo_mode,
            "CRON_GEO_TIME": cron_geo_time
        }
        
        write_env(updates)
        
        update_cron("# JOB_SUB", updates['CRON_SUB_SCHED'], f"bash {SCRIPT_DIR}/update_subscription.sh >/dev/null 2>&1", updates['CRON_SUB_ENABLED'] == 'true')
        update_cron("# JOB_GEO", updates['CRON_GEO_SCHED'], f"bash {SCRIPT_DIR}/update_geo.sh >/dev/null 2>&1", updates['CRON_GEO_ENABLED'] == 'true')
        
        return jsonify({"success": True, "message": "配置已成功保存！"})

if __name__ == '__main__':
    env = read_env()
    try:
        port = int(env.get('WEB_PORT', 7838))
    except:
        port = 7838
    app.run(host='0.0.0.0', port=port)
