from flask import Flask, render_template, request, jsonify, Response, redirect, session
from functools import wraps
from datetime import timedelta
from collections import deque
import subprocess
import base64
import logging
import os
import re
import secrets
import shlex
import glob
import json
import shutil
import tempfile
import threading
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
PANEL_VERSION = "0.1.25"
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
# 日志只读文件尾部，避免把几百 MB 的日志整个读进内存
LOG_TAIL_BYTES = 256 * 1024
# 面板升级包的下载上限，防止异常源把磁盘写满
DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
# config.yaml 备份默认保留数量，.env 的 BACKUP_KEEP_COUNT 可覆盖
DEFAULT_BACKUP_KEEP_COUNT = 10
# 登录失败限流：同一 IP 连续失败 5 次后锁定 60 秒
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
BUSY_MESSAGE = "另一个操作正在进行中，请稍后再试。"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mihomo-manager")

app = Flask(__name__)
# 登录态 30 天：有登录限流和改密后轮换密钥兜底，不需要再签发一年期 cookie
app.permanent_session_lifetime = timedelta(days=30)
# Cookie 只走同站请求且脚本不可读；请求体限制 1 MiB，/api/rule-sync 等接口不需要更大
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

# 配置写入 + 重启串行化。RLock 允许同一请求里外层路由和内层 update_mihomo_sync_block 先后加锁。
CONFIG_LOCK = threading.RLock()

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

def read_recent_log_lines(path, limit=100, tail_bytes=LOG_TAIL_BYTES):
    """只读文件末尾 tail_bytes 字节再取最后 limit 行，日志再大也不会整个读进内存。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - tail_bytes)
            f.seek(start)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        lines = text.splitlines(True)
        # 从文件中间开始读时第一行多半是半截的，丢掉
        if start > 0 and lines:
            lines = lines[1:]
        return "".join(deque(lines, maxlen=limit))
    except Exception as e:
        return str(e)

def yaml_scalar_text(raw):
    """取一行 YAML 标量的值：去掉一层匹配的引号，未加引号时去掉行尾 ' # 注释'。"""
    raw = str(raw or "").strip()
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    for quote in ("\"", "'"):
        if raw.startswith(quote):
            end = raw.find(quote, 1)
            if end > 0:
                return raw[1:end]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()

def config_value(key):
    """只认顶层（0 缩进）的 key:，嵌套在别的段落里的同名键（比如 proxy 节点里的 secret:）不算。"""
    if not os.path.exists(CONFIG_FILE):
        return ""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                match = re.match(rf"^{re.escape(key)}\s*:(.*)$", line.rstrip("\r\n"))
                if match:
                    return yaml_scalar_text(match.group(1))
    except Exception:
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
        except Exception: pass
    return env_data

ANSI_C_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                  "\\": "\\", "'": "'", '"': '"', "?": "?"}

def decode_ansi_c_quoted(body):
    """解码 bash 的 $'...' 字面量（printf %q 对含换行/控制字符的值会输出这种形式）。"""
    def repl(match):
        esc = match.group(0)[1:]
        if esc[0] in ANSI_C_ESCAPES:
            return ANSI_C_ESCAPES[esc[0]]
        if esc[0] == "x":
            return chr(int(esc[1:], 16))
        if esc[0] in "uU":
            return chr(int(esc[1:], 16))
        return chr(int(esc, 8))
    return re.sub(r"\\(?:x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|.)", repl, body)

def strip_one_quote_layer(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw

def parse_env_line(line):
    """解析 .env 的一行 KEY=value。

    先按 shell 语法（shlex）解析，这样 KEY='a b'、KEY=a\\ b、KEY="x" # 注释 都正确；
    未加引号的值里的 # 不是注释起点（shlex 的 comments=True 会把它截断，所以这里不用）。
    bash printf %q 写出的 $'...' 形式 shlex 不认识，单独解码；引号不配对时退回简单切分。
    """
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in stripped:
        return None
    key, _, raw = stripped.partition('=')
    key = key.strip()
    if key.startswith('export '):
        key = key[len('export '):].strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        return None
    raw = raw.strip()
    if raw.startswith("$'"):
        match = re.match(r"\$'((?:[^'\\]|\\.)*)'", raw)
        body = match.group(1) if match else raw[2:].rstrip("'")
        return key, decode_ansi_c_quoted(body)
    try:
        parts = shlex.split(raw, comments=False, posix=True)
    except ValueError:
        return key, strip_one_quote_layer(raw)
    if not parts:
        return key, ""
    return key, parts[0]

def env_value_for_shell(value):
    normalized = str(value if value is not None else '')
    normalized = normalized.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n')
    return shlex.quote(normalized)

def env_line(key, value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        raise ValueError(f"Invalid env key: {key}")
    return f'{key}={env_value_for_shell(value)}\n'

def write_env(updates):
    """.env 里有密码和各类密钥：先写 0600 的临时文件，再原子替换，不留半截文件也不留宽权限。"""
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    out = []
    keys = set()
    for line in lines:
        parsed = parse_env_line(line)
        if parsed and parsed[0] in updates:
            out.append(env_line(parsed[0], updates[parsed[0]]))
            keys.add(parsed[0])
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for k, v in updates.items():
        if k not in keys:
            out.append(env_line(k, v))

    env_dir = os.path.dirname(ENV_FILE)
    os.makedirs(env_dir, exist_ok=True)
    tmp_path = os.path.join(env_dir, f".env.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write("".join(out))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, ENV_FILE)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass

def rotate_session_secret():
    """换掉 Flask 签名密钥，所有已签发的会话立即失效。"""
    secret = secrets.token_urlsafe(48)
    write_env({"WEB_SESSION_SECRET": secret})
    app.secret_key = secret
    return secret

def ensure_session_secret():
    env = read_env()
    secret = os.environ.get('WEB_SESSION_SECRET') or env.get('WEB_SESSION_SECRET')
    if not secret or secret == "mihomo-manager-secret":
        secret = rotate_session_secret()
    app.secret_key = secret

ensure_session_secret()

def web_credentials():
    env = read_env()
    user = os.environ.get('WEB_USER') or env.get('WEB_USER') or ''
    password = os.environ.get('WEB_SECRET') or env.get('WEB_SECRET') or ''
    return user, password

def check_creds(username, password):
    # 没有配置账号就拒绝登录，绝不退回 admin/admin 这种默认口令
    valid_user, valid_pass = web_credentials()
    if not valid_user or not valid_pass:
        log.error("登录被拒绝：%s 缺少 WEB_USER / WEB_SECRET，请先在 .env 里配置账号。", ENV_FILE)
        return False
    username = str(username or "")
    password = str(password or "")
    return secrets.compare_digest(username.encode(), valid_user.encode()) and secrets.compare_digest(password.encode(), valid_pass.encode())

LOGIN_FAILURES = {}
LOGIN_FAILURES_LOCK = threading.Lock()

def client_ip():
    return (request.remote_addr or "unknown").strip()

def login_locked(ip):
    """返回该 IP 还要等多少秒才能再试，0 表示没锁。"""
    now = time.time()
    with LOGIN_FAILURES_LOCK:
        entry = LOGIN_FAILURES.get(ip)
        if not entry:
            return 0
        failures, lock_until = entry
        if lock_until > now:
            return int(lock_until - now) + 1
        if failures >= LOGIN_MAX_FAILURES:
            LOGIN_FAILURES.pop(ip, None)
        return 0

def record_login_failure(ip):
    now = time.time()
    with LOGIN_FAILURES_LOCK:
        # 顺手清掉早已过期的记录，避免字典无限增长
        for key in [k for k, (_, until) in LOGIN_FAILURES.items() if until and until < now - LOGIN_LOCKOUT_SECONDS]:
            LOGIN_FAILURES.pop(key, None)
        failures, _ = LOGIN_FAILURES.get(ip, (0, 0))
        failures += 1
        lock_until = now + LOGIN_LOCKOUT_SECONDS if failures >= LOGIN_MAX_FAILURES else 0
        LOGIN_FAILURES[ip] = (failures, lock_until)
        return failures

def clear_login_failures(ip):
    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.pop(ip, None)

def is_valid_web_username(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", str(value or "")))

def update_cron(job_id, schedule, command, enabled):
    """返回 (ok, message)。写 crontab 失败要让调用方知道，不能静默吞掉。"""
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        current_cron = res.stdout.strip().split('\n') if res.stdout else []
        new_cron = []
        for line in current_cron:
            if job_id not in line and line.strip() != "":
                new_cron.append(line)
        if enabled:
            new_cron.append(f"{schedule} {command} {job_id}")
        cron_str = "\n".join(new_cron) + "\n"
        result = subprocess.run(["crontab", "-"], input=cron_str, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            log.error("写入 crontab 失败 (%s): %s", job_id, detail)
            return False, f"写入定时任务失败 ({job_id.strip('# ')}): {detail or 'crontab 返回非零'}"
        return True, ""
    except Exception as e:
        log.error("写入 crontab 异常 (%s): %s", job_id, e)
        return False, f"写入定时任务失败 ({job_id.strip('# ')}): {e}"

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
    try:
        result = subprocess.run([checker, "-t", "-d", MIHOMO_DIR, "-f", path], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "配置校验超时（30 秒），请检查配置里的远程资源是否可达。"
    except Exception as e:
        return False, f"配置校验无法执行：{e}"
    return result.returncode == 0, result.stdout + result.stderr

def backup_keep_count():
    try:
        count = int(read_env().get("BACKUP_KEEP_COUNT", DEFAULT_BACKUP_KEEP_COUNT))
    except (TypeError, ValueError):
        count = DEFAULT_BACKUP_KEEP_COUNT
    return max(1, count)

def prune_config_backups(keep=None):
    """只保留最新的 keep 份 config 备份（面板和 update_subscription.sh 共用同一批文件名模式）。"""
    keep = backup_keep_count() if keep is None else keep
    patterns = (f"{BACKUP_DIR}/config_*.yaml", f"{BACKUP_DIR}/config.before-rule-sync.*.yaml")
    backups = [path for pattern in patterns for path in glob.glob(pattern) if os.path.isfile(path)]
    backups.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for path in backups[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass

def write_tmp_config(text, suffix):
    """把候选配置写到 MIHOMO_DIR 下的随机临时文件（mihomo -t 需要和 config.yaml 同目录才能解析相对路径）。"""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=MIHOMO_DIR, prefix="config.yaml.", suffix=suffix, delete=False) as f:
        f.write(text)
        return f.name

def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass

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
        prune_config_backups()
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

PROXY_GROUP_NAME_RE = re.compile(r"^\s*-\s*\{?\s*name\s*:\s*(?:\"([^\"]*)\"|'([^']*)'|([^,}#\n]+))")

def proxy_group_names(text):
    """列出顶层 proxy-groups: 下每个策略组的 name（支持 - {name: x, ...} 流式和 - name: x 块式）。"""
    lines = text.splitlines()
    start = find_yaml_top_level_key(lines, "proxy-groups")
    if start < 0:
        return []
    names = []
    for line in lines[start + 1:section_end_index(lines, start)]:
        match = PROXY_GROUP_NAME_RE.match(line)
        if not match:
            continue
        name = next((group for group in match.groups() if group is not None), "").strip()
        if name:
            names.append(name)
    return names

def proxy_policy_name(text):
    """同步规则里"强制代理"要指向的策略组：只在真实存在的 proxy-groups 条目里挑，
    注释/规则行里提到的名字不算（否则一行注释就能把规则指向不存在的组，配置校验失败）。"""
    names = proxy_group_names(text)
    for preferred in ("♻️ 自动选择", "🚀 默认代理", "Final", "GLOBAL"):
        if preferred in names:
            return preferred
    return names[0] if names else "PROXY"

def read_mihomo_sync_rules(text=None):
    if text is None:
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

def build_mihomo_sync_rule_lines(rule_contents, proxy_policy, item_indent=""):
    block = [f"{item_indent}{RULE_SYNC_BEGIN}\n"]
    for domain in normalize_rule_domains(rule_contents.get("force-cn", "")):
        block.append(f"{item_indent}- DOMAIN-SUFFIX,{domain},DIRECT\n")
    for domain in normalize_rule_domains(rule_contents.get("force-nocn", "")):
        block.append(f"{item_indent}- DOMAIN-SUFFIX,{domain},{proxy_policy}\n")
    block.append(f"{item_indent}{RULE_SYNC_END}\n")
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

def render_sync_blocks(text, rule_contents):
    lines = text.splitlines(True)
    if not lines:
        lines = ["rules:\n"]
    proxy_policy = proxy_policy_name(text)

    start = next((idx for idx, line in enumerate(lines) if line.strip() == RULE_SYNC_BEGIN), -1)
    if start >= 0:
        end = next((idx for idx in range(start + 1, len(lines)) if lines[idx].strip() == RULE_SYNC_END), start)
        # 沿用已有标记行的缩进
        block = build_mihomo_sync_rule_lines(rule_contents, proxy_policy, line_indent(lines[start]))
        lines[start:end + 1] = block
    else:
        rules_index = find_yaml_top_level_key(lines, "rules")
        if rules_index < 0:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append("rules:\n")
            rules_index = len(lines) - 1
        # rules: 下的列表项可能是 0 格或 2 格缩进，必须和现有项保持一致，否则 YAML 无效
        item_indent = list_item_indent_after_key(lines, rules_index)
        block = build_mihomo_sync_rule_lines(rule_contents, proxy_policy, item_indent)
        lines[rules_index + 1:rules_index + 1] = block
    lines = update_fake_ip_filter_block(lines, rule_contents)
    return "".join(lines)


def reapply_sync_blocks(source_path, target_path):
    """把 source 配置里的同步规则块写到 target 配置里。订阅更新会重新生成 config.yaml，
    不做这一步，面板里保存的强制直连/强制代理规则每次更新都会丢。"""
    with open(source_path, "r", encoding="utf-8") as f:
        rule_contents = read_mihomo_sync_rules(f.read())
    if not any(rule_contents.values()):
        return False
    with open(target_path, "r", encoding="utf-8") as f:
        target_text = f.read()
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(render_sync_blocks(target_text, rule_contents))
    return True


def update_mihomo_sync_block(rule_contents):
    if not CONFIG_LOCK.acquire(blocking=False):
        return False, BUSY_MESSAGE
    try:
        new_text = render_sync_blocks(read_config_text(), rule_contents)
        tmp_file = write_tmp_config(new_text, ".rulesync")
        try:
            ok, message = validate_config(tmp_file)
        finally:
            remove_quietly(tmp_file)
        if not ok:
            return False, "规则写入后配置校验失败，已取消保存：\n" + message
        write_config_text(new_text)
        return True, "规则已写入 mihomo 配置"
    finally:
        CONFIG_LOCK.release()

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

def schedule_mihomo_restart():
    subprocess.Popen(
        ["sh", "-c", "sleep 1; systemctl restart mihomo"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

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
    if not CONFIG_LOCK.acquire(blocking=False):
        return False, BUSY_MESSAGE
    try:
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
        schedule_mihomo_restart()
        return True, "已同步规则：" + ", ".join(applied) + "，mihomo 将在后台重启"
    finally:
        CONFIG_LOCK.release()

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
    except Exception:
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
        lines = read_recent_log_lines(LOG_FILE, 200).splitlines()
        for line in lines:
            match = re.search(r"level=([a-zA-Z]+)", line)
            if not match:
                continue
            level = match.group(1).lower()
            if level in ("warning", "warn"):
                levels["warn"] += 1
            elif level in levels:
                levels[level] += 1
    except Exception:
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

def github_proxy_prefix():
    """GitHub 代理前缀来自 .env 的 GH_PROXY，留空表示不用代理。只接受 http(s) 前缀。"""
    prefix = str(read_env().get("GH_PROXY", "") or "").strip()
    if not prefix:
        return ""
    if not prefix.startswith(("http://", "https://")):
        return ""
    return prefix if prefix.endswith("/") else prefix + "/"

def github_candidate_urls(url):
    """先直连 GitHub，失败再退到 GH_PROXY。代理是第三方，绝不能排在官方源前面。"""
    if not url:
        return []
    prefix = github_proxy_prefix()
    urls = [url]
    if prefix and url.startswith(("https://github.com/", "https://raw.githubusercontent.com/")):
        urls.append(prefix + url)
    return urls

def read_url_text(urls, timeout=15, max_bytes=DOWNLOAD_MAX_BYTES):
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mihomo-web-manager"})
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"响应超过 {max_bytes // (1024 * 1024)} MB 上限")
            return True, data.decode("utf-8", "replace"), url
        except Exception as e:
            last_error = str(e)
    return False, last_error, ""

def download_file(urls, output, timeout=30, max_bytes=DOWNLOAD_MAX_BYTES):
    last_error = ""
    for url in urls:
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "mihomo-web-manager"})
            with urlrequest.urlopen(req, timeout=timeout) as resp, open(output, "wb") as f:
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError(f"文件大小 {int(declared) // (1024 * 1024)} MB 超过 {max_bytes // (1024 * 1024)} MB 上限")
                written = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"下载超过 {max_bytes // (1024 * 1024)} MB 上限，已中止")
                    f.write(chunk)
            return True, url
        except Exception as e:
            last_error = str(e)
            remove_quietly(output)
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
    # 顺序：GitHub API -> raw 直连 -> GH_PROXY 代理（仅当 .env 配置了 GH_PROXY）
    ok, text, source = read_url_text([contents_url] + github_candidate_urls(raw_url), timeout=15)
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
        ("/etc/logrotate.d/mihomo", "etc/logrotate.d/mihomo", "file", 0o644),
    ]

def download_panel_source(tmpdir):
    settings = panel_repo_settings()
    archive_url = github_archive_url(settings["repo_url"], settings["branch"])
    if not archive_url:
        return False, "当前只支持 GitHub 仓库地址。", None, settings
    zip_path = os.path.join(tmpdir, "mihomo-panel.zip")
    ok, source = download_file(github_candidate_urls(archive_url), zip_path)
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

def json_body():
    """只接受 JSON 对象；数组/字符串/解析失败一律当空字典，调用方不用再逐个 isinstance。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}

def is_xhr_request():
    return request.headers.get("X-Requested-With", "") == "XMLHttpRequest"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api'): return jsonify({"error": "Unauthorized"}), 401
            return redirect('/login')
        # 写操作必须带自定义头：浏览器跨站表单/简单请求加不了这个头，配合 SameSite=Lax 挡住 CSRF
        if request.path.startswith('/api') and request.method not in ("GET", "HEAD", "OPTIONS") and not is_xhr_request():
            return jsonify({"success": False, "message": "缺少 X-Requested-With 请求头"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = client_ip()
        wait = login_locked(ip)
        if wait > 0:
            return render_template('login.html', error=f"登录失败次数过多，请 {wait} 秒后再试"), 429
        if check_creds(request.form.get('username'), request.form.get('password')):
            clear_login_failures(ip)
            session['logged_in'] = True
            session.permanent = True
            return redirect('/')
        failures = record_login_failure(ip)
        log.warning("登录失败 ip=%s 连续失败 %d 次", ip, failures)
        return render_template('login.html', error="用户名或密码错误"), 401

    if session.get('logged_in'):
        return redirect('/')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
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
    action = json_body().get('action')
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
    if action != 'upgrade_panel' and action not in control_actions:
        return jsonify({"success": False, "message": "未知指令"})
    # 升级面板 / 更新订阅 / 重启内核都会动配置或服务，和规则写入互斥
    if not CONFIG_LOCK.acquire(blocking=False):
        return jsonify({"success": False, "message": BUSY_MESSAGE})
    try:
        if action == 'upgrade_panel':
            ok, message, should_reload = upgrade_panel()
            return jsonify({"success": ok, "message": message, "reload_after": 5 if should_reload else 0})
        s, m = run_args(control_actions[action], timeout=180)
        return jsonify({"success": s, "message": m})
    finally:
        CONFIG_LOCK.release()

@app.route('/api/config', methods=['GET', 'POST'])
@login_required
def handle_config():
    if request.method == 'GET':
        c = ""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE,'r', encoding='utf-8') as f:
                    c = f.read()
            except Exception:
                pass
        return jsonify({"content": c})
    if request.method == 'POST':
        content = json_body().get('content') or ''
        if not isinstance(content, str):
            return jsonify({"success": False, "message": "配置内容必须是文本"}), 400
        if not CONFIG_LOCK.acquire(blocking=False):
            return jsonify({"success": False, "message": BUSY_MESSAGE})
        tmp_file = None
        try:
            tmp_file = write_tmp_config(content, ".webcheck")
            ok, message = validate_config(tmp_file)
            if not ok:
                return jsonify({"success": False, "message": "配置校验失败，未保存：\n" + message})
            if os.path.exists(CONFIG_FILE):
                os.makedirs(BACKUP_DIR, exist_ok=True)
                shutil.copy2(CONFIG_FILE, f"{BACKUP_DIR}/config_{time.strftime('%Y%m%d%H%M%S')}.yaml")
                prune_config_backups()
            os.replace(tmp_file, CONFIG_FILE)
            tmp_file = None
            return jsonify({"success": True, "message": "配置已保存"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})
        finally:
            if tmp_file:
                remove_quietly(tmp_file)
            CONFIG_LOCK.release()

@app.route('/api/logs')
@login_required
def get_logs():
    if not os.path.exists(LOG_FILE): return jsonify({"logs": "日志未生成"})
    logs = read_recent_log_lines(LOG_FILE, 100)
    return jsonify({"logs": logs if logs else "暂无日志"})

@app.route('/api/account', methods=['POST'])
@login_required
def update_account_credentials():
    data = json_body()
    _, valid_pass = web_credentials()
    if not valid_pass:
        return jsonify({"success": False, "message": "服务端未配置 WEB_SECRET，请先在 .env 里设置账号。"})
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
    # 换签名密钥让其他浏览器里的会话全部失效，不然旧会话改完密码还能继续用
    rotate_session_secret()
    session.clear()
    return jsonify({"success": True, "message": "账号已更新，请使用新凭据重新登录。", "reload_after": 1})

@app.route("/api/rule-sync-settings", methods=["GET", "POST"])
@login_required
def api_rule_sync_settings():
    if request.method == "GET":
        return jsonify(read_sync_settings())
    ok, message = write_sync_settings(json_body())
    return jsonify({"success": ok, "message": message, **read_sync_settings()})

@app.route("/api/rule-sync-test", methods=["POST"])
@login_required
def api_rule_sync_test():
    ok, message, results = test_sync_peers(json_body())
    return jsonify({"success": ok, "message": message, "results": results})

def sync_token_matches(provided, expected):
    if not expected or not provided:
        return False
    return secrets.compare_digest(str(provided).encode("utf-8"), str(expected).encode("utf-8"))

@app.route("/api/rule-sync", methods=["POST"])
def api_rule_sync():
    # 这个接口不走登录态，只认同步密钥。请求头里有密钥就先比对，不合法的请求连 JSON 都不解析。
    expected = read_env().get("RULE_SYNC_TOKEN", "")
    header_token = request.headers.get("X-Mosdns-Sync-Token", "")
    if header_token and not sync_token_matches(header_token, expected):
        return jsonify({"success": False, "message": "同步密钥错误"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "请求体必须是 JSON 对象"}), 400
    provided = header_token or str(data.get("token") or "")
    if not sync_token_matches(provided, expected):
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
    content = json_body().get("content", "")
    if not CONFIG_LOCK.acquire(blocking=False):
        return jsonify({"success": False, "message": BUSY_MESSAGE})
    try:
        saved, save_message = save_rule_content(rule_id, content)
        if not saved:
            return jsonify({"success": False, "message": save_message})
        ok, message = restart_mihomo()
    finally:
        CONFIG_LOCK.release()
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
            "web_user": os.environ.get('WEB_USER') or e.get('WEB_USER', ''),
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
        d = json_body()
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

        cron_errors = []
        for ok, message in (
            update_cron("# JOB_SUB", updates['CRON_SUB_SCHED'], f"bash {SCRIPT_DIR}/update_subscription.sh >/dev/null 2>&1", updates['CRON_SUB_ENABLED'] == 'true'),
            update_cron("# JOB_GEO", updates['CRON_GEO_SCHED'], f"bash {SCRIPT_DIR}/update_geo.sh >/dev/null 2>&1", updates['CRON_GEO_ENABLED'] == 'true'),
        ):
            if not ok:
                cron_errors.append(message)
        if cron_errors:
            return jsonify({"success": False, "message": "设置已写入 .env，但定时任务更新失败：\n" + "\n".join(cron_errors)})

        return jsonify({"success": True, "message": "配置已成功保存！"})

if __name__ == '__main__':
    env = read_env()
    try:
        port = int(env.get('WEB_PORT', 7838))
    except Exception:
        port = 7838
    app.run(host='0.0.0.0', port=port)
