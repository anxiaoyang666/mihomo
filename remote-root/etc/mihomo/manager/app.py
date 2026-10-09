from flask import Flask, render_template, request, jsonify, Response, redirect, session
from functools import wraps
from datetime import timedelta
from collections import deque
import subprocess
import atexit
import base64
import calendar
import fcntl
import logging
import os
import re
import secrets
import shlex
import glob
import ipaddress
import json
import shutil
import signal
import ssl
import sys
import tempfile
import threading
import time
import zipfile
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, unquote, urlencode, urlsplit
try:
    # 网关上由 install.sh 装 python3-yaml；没有时无法比较配置，改配置一律保守重启
    import yaml
except ImportError:
    yaml = None

# 通知标题/正文/去重的唯一实现，和 scripts/notify.sh --event 共用（只依赖标准库）。
# 测试会用 exec 载入 app.py（没有 __file__），所以用代码对象的文件名找同目录。
_APP_DIR = os.path.dirname(os.path.abspath(sys._getframe().f_code.co_filename))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
import notify_format

MIHOMO_DIR = "/etc/mihomo"
SCRIPT_DIR = "/etc/mihomo/scripts"
ENV_FILE = f"{MIHOMO_DIR}/.env"
CONFIG_FILE = f"{MIHOMO_DIR}/config.yaml"
LOG_FILE = "/var/log/mihomo.log"
BACKUP_DIR = f"{MIHOMO_DIR}/backup"
# update_subscription.sh 每次运行写一行：<unix 时间>\t<ok|failed>\t<说明>
SUBSCRIPTION_STATE_FILE = f"{MIHOMO_DIR}/.last_subscription"
SUBSCRIPTION_LOG = "/var/log/mihomo-subscription.log"
GEO_LOG = "/var/log/mihomo-geo.log"
MANAGER_DIR = f"{MIHOMO_DIR}/manager"
PANEL_VERSION = "0.1.51"
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
RULE_PROVIDERS_BEGIN = "# MOSCTL_MIHOMO_RULE_PROVIDERS_BEGIN"
RULE_PROVIDERS_END = "# MOSCTL_MIHOMO_RULE_PROVIDERS_END"
# 同步规则的域名放在规则文件里，config.yaml 只通过 file 类型的 rule-provider 引用；
# 改规则时写文件再 PUT /providers/rules/<name> 热刷新，不用重启 mihomo
RULES_DIR = f"{MIHOMO_DIR}/rules"
RULE_PROVIDER_NAMES = {"force-cn": "mosctl_force_cn", "force-nocn": "mosctl_force_nocn"}
RULE_PROVIDER_FILES = {
    "force-cn": f"{RULES_DIR}/mosctl-force-cn.yaml",
    "force-nocn": f"{RULES_DIR}/mosctl-force-nocn.yaml",
}
RULES_UNCHANGED_MESSAGE = "规则内容没有变化，未重启"
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

def migrate_cron_commands():
    """老面板写的定时任务把输出丢到 /dev/null，升级后改成写日志；只有需要改时才动 crontab。"""
    rewrites = {
        f"update_subscription.sh >/dev/null 2>&1 # JOB_SUB": f"update_subscription.sh >> {SUBSCRIPTION_LOG} 2>&1 # JOB_SUB",
        f"update_geo.sh >/dev/null 2>&1 # JOB_GEO": f"update_geo.sh >> {GEO_LOG} 2>&1 # JOB_GEO",
    }
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        if res.returncode != 0 or not res.stdout:
            return False
        updated = res.stdout
        for old, new in rewrites.items():
            updated = updated.replace(old, new)
        if updated == res.stdout:
            return False
        subprocess.run(["crontab", "-"], input=updated, capture_output=True, text=True, timeout=15)
        log.info("已把订阅/Geo 定时任务的输出改为写入日志")
        return True
    except Exception as e:
        log.warning("迁移定时任务失败：%s", e)
        return False

migrate_cron_commands()

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

# ===== 本地直连网段（LOCAL_CIDR）=====
# 四个站点共用一份托管配置，里面有 IP-CIDR,<各站点网段>,Home-xx 规则（经 SS 隧道去别的站点）。
# 在某个站点上，它自己的网段必须走 DIRECT 并从 TUN 排除，否则访问本地局域网会绕进指向自己的隧道。
# update_subscription.sh 的防回环阶段 import 这里的函数（失败时用脚本里的同款备份实现），两边结论一致。
LOCAL_NETWORKS_MAX = 8
LOCAL_NETWORKS_ALLOWED_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
LOCAL_NETWORKS_ALLOWED_V6 = (ipaddress.ip_network("fc00::/7"),)
# TUN 自己的地址段（fake-ip / tun 网卡），自动识别时跳过
LOCAL_NETWORKS_TUN_V4 = ipaddress.ip_network("198.18.0.0/15")
LOCAL_NETWORKS_DETECT_TTL = 30
_local_networks_detect_cache = {"at": 0.0, "value": None}

def _parse_local_network(raw):
    """单个条目 → (ip_network, None) 或 (None, 错误信息)。"""
    shown = raw if len(raw) <= 40 else raw[:40] + "…"
    bad = f"无法识别「{shown}」，请填写形如 10.10.20.0 或 10.10.20.0/24 的网段"
    try:
        if "/" in raw:
            net = ipaddress.ip_network(raw, strict=False)
        elif re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", raw):
            net = ipaddress.ip_network(raw + ".0/24")
        else:
            addr = ipaddress.ip_address(raw)
            net = ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 64}", strict=False)
    except ValueError:
        return None, bad
    if net.version == 4:
        if net.prefixlen < 8:
            return None, f"「{net}」范围太大，IPv4 网段至少要 /8"
        allowed = LOCAL_NETWORKS_ALLOWED_V4
    else:
        if net.prefixlen < 32:
            return None, f"「{net}」范围太大，IPv6 网段至少要 /32"
        allowed = LOCAL_NETWORKS_ALLOWED_V6
    if not any(net.subnet_of(a) for a in allowed):
        return None, (f"「{net}」不是局域网地址，只能填写局域网网段"
                      "（10.x、172.16-31.x、192.168.x、100.64-127.x 或 IPv6 fd00::/8）")
    return net, None

def normalize_local_networks(text):
    """把面板里填的本地直连网段规范化为 (["10.10.20.0/24", ...], None)，出错时返回 ([], 错误信息)。

    支持逗号/中文逗号/空白/换行分隔；a.b.c.d → 所在 /24，a.b.c → a.b.c.0/24，带掩码的清掉主机位；
    IPv6 不带前缀按 /64。去重保序，最多 LOCAL_NETWORKS_MAX 个。空输入返回 ([], None)，表示自动识别。
    """
    if text is None:
        return [], None
    if isinstance(text, (list, tuple)):
        text = ",".join(str(item) for item in text)
    networks = []
    for raw in re.split(r"[\s,，、;；]+", str(text)):
        if not raw:
            continue
        net, error = _parse_local_network(raw)
        if error:
            return [], error
        if str(net) not in networks:
            networks.append(str(net))
    if len(networks) > LOCAL_NETWORKS_MAX:
        return [], f"最多填写 {LOCAL_NETWORKS_MAX} 个网段（现在有 {len(networks)} 个）"
    return networks, None

def parse_ip_addr_networks(output):
    """从 `ip -4 -o addr show scope global` 的输出取本机局域网网段；跳过 TUN（Meta/tun 网卡、198.18/15）。"""
    networks = []
    for line in str(output or "").splitlines():
        match = re.match(r"^\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not match:
            continue
        ifname = match.group(1).split("@")[0].lower()
        if "meta" in ifname or ifname.startswith(("tun", "utun")):
            continue
        try:
            iface = ipaddress.ip_interface(match.group(2))
        except ValueError:
            continue
        if iface.ip in LOCAL_NETWORKS_TUN_V4:
            continue
        net, error = _parse_local_network(str(iface.network))
        if error or str(net) in networks:
            continue
        networks.append(str(net))
    return networks[:LOCAL_NETWORKS_MAX]

def detect_local_networks(use_cache=True):
    now = time.time()
    cache = _local_networks_detect_cache
    if use_cache and cache["value"] is not None and now - cache["at"] < LOCAL_NETWORKS_DETECT_TTL:
        return list(cache["value"])
    try:
        result = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=5)
        networks = parse_ip_addr_networks(result.stdout) if result.returncode == 0 else []
    except Exception:
        networks = []
    cache.update(at=now, value=list(networks))
    return networks

def effective_local_networks(stored, detect=None):
    """返回 (实际使用的网段列表, 是否自动识别, 错误信息)。

    填了且合法 → 用填的；留空 → 自动识别本机网段；填的内容不合法（老版本没校验时存进去的）→ 自动识别并带上错误信息。
    """
    networks, error = normalize_local_networks(stored)
    if networks and not error:
        return networks, False, None
    detected = (detect or detect_local_networks)()
    return list(detected), True, error

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

RULE_DOMAIN_RE = re.compile(r"[A-Za-z0-9*_.-]+\.[A-Za-z0-9_.-]+")
# mosctl（mosdns domain_set）的写法：domain:/full: 前缀、行尾 # 注释都合法；keyword:/regexp: mihomo 的域名规则集表达不了
RULE_STRIP_PREFIXES = ("domain-suffix,", "domain,", "domain:", "full:")
RULE_MOSDNS_ONLY_PREFIXES = ("keyword:", "regexp:")

def parse_rule_domains(content):
    """返回 (域名列表, 问题列表)。问题是 (行号, 原文, 类型)：类型 space / url / invalid 是写错了，
    mosdns_only 是 keyword:/regexp:（mosdns 能用，mihomo 用不了）。"""
    domains, problems = [], []
    for number, line in enumerate(str(content or "").replace("|", "\n").splitlines(), 1):
        item = line.split("#", 1)[0].strip()
        if not item:
            continue
        if len(item.split()) != 1:
            problems.append((number, item, "space"))
            continue
        lower = item.lower()
        if lower.startswith(RULE_MOSDNS_ONLY_PREFIXES):
            problems.append((number, item, "mosdns_only"))
            continue
        for prefix in RULE_STRIP_PREFIXES:
            if lower.startswith(prefix):
                item = item[len(prefix):]
                break
        if "://" in item or "/" in item:
            problems.append((number, item, "url"))
            continue
        item = item.lstrip(".")
        if RULE_DOMAIN_RE.fullmatch(item):
            domains.append(item.lower())
        else:
            problems.append((number, item, "invalid"))
    return list(dict.fromkeys(domains)), problems

def normalize_rule_domains(content):
    return parse_rule_domains(content)[0]

def rule_content_problem(content):
    """本机保存时的检查：写错的行直接报错（同步给 mosctl 也会被拒）；只 mosdns 能用的写法放行但提示。
    返回 (错误, 提示)。"""
    _, problems = parse_rule_domains(content)
    for number, item, kind in problems:
        shown = item if len(item) <= 60 else item[:60] + "…"
        if kind == "space":
            return f"第 {number} 行只能写一个域名，不能有空格（注释请用 # 开头）", ""
        if kind == "url":
            host = urlsplit(item if "://" in item else "http://" + item).hostname or ""
            return f"第 {number} 行是网址，请只写域名" + (f"，例如 {host}" if host else "") + f"：{shown}", ""
        if kind == "invalid":
            return f"第 {number} 行不是合法域名：{shown}", ""
    only = [str(number) for number, _item, kind in problems if kind == "mosdns_only"]
    note = f"第 {'、'.join(only)} 行是 keyword:/regexp: 规则，mihomo 不支持，只在 mosdns 生效" if only else ""
    return "", note

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

def sync_blocks_mode(text):
    """config.yaml 里同步规则的形态：provider（规则集 + 规则文件）、legacy（域名直接内联）、none（还没有）。"""
    if RULE_PROVIDERS_BEGIN in text and f"RULE-SET,{RULE_PROVIDER_NAMES['force-cn']}," in text:
        return "provider"
    if RULE_SYNC_BEGIN in text:
        return "legacy"
    return "none"

def read_legacy_inline_sync_rules(text):
    """旧格式：域名以 - DOMAIN-SUFFIX,x,策略 的形式内联在 rules: 的标记块里。"""
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
    return {key: rule_content_text(value) for key, value in rules.items()}

def rule_content_text(domains):
    return "\n".join(domains) + ("\n" if domains else "")

def read_rule_provider_file(path):
    """读 payload: 列表（- '+.example.com'），还原成每行一个域名。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    domains = []
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if not match:
            continue
        item = match.group(1).strip("'\"")
        if item.startswith("+."):
            item = item[2:]
        domains.append(item)
    return rule_content_text(normalize_rule_domains("\n".join(domains)))

def read_rule_provider_files():
    return {rule_id: read_rule_provider_file(path) for rule_id, path in RULE_PROVIDER_FILES.items()}

def render_rule_provider_file(content):
    domains = normalize_rule_domains(content)
    if not domains:
        return "payload: []\n"
    # 单引号包起来：域名可能以 * 开头，裸写会被 YAML 当成别名
    return "payload:\n" + "".join(f"  - '+.{domain}'\n" for domain in domains)

def write_text_atomic(path, text, mode=0o644):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False) as f:
        f.write(text)
        tmp_path = f.name
    try:
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        remove_quietly(tmp_path)
        raise

def write_rule_provider_files(rule_contents):
    for rule_id, path in RULE_PROVIDER_FILES.items():
        write_text_atomic(path, render_rule_provider_file(rule_contents.get(rule_id, "")))

def snapshot_rule_provider_files():
    """写规则文件前的原样内容（None 表示文件原本不存在），校验失败时用来还原。"""
    snapshot = {}
    for path in RULE_PROVIDER_FILES.values():
        try:
            with open(path, "r", encoding="utf-8") as f:
                snapshot[path] = f.read()
        except OSError:
            snapshot[path] = None
    return snapshot

def restore_rule_provider_files(snapshot):
    for path, text in snapshot.items():
        try:
            if text is None:
                remove_quietly(path)
            else:
                write_text_atomic(path, text)
        except OSError:
            pass

def read_mihomo_sync_rules(text=None):
    """当前生效的同步规则：规则集格式读规则文件，旧格式读 config.yaml 里的内联块。"""
    if text is None:
        text = read_config_text()
    if sync_blocks_mode(text) == "provider":
        return read_rule_provider_files()
    return read_legacy_inline_sync_rules(text)

def rule_domain_set(content):
    return set(normalize_rule_domains(content))

def sync_rules_unchanged(current, new_contents):
    return all(rule_domain_set(current.get(rule_id, "")) == rule_domain_set(new_contents.get(rule_id, "")) for rule_id in SYNCABLE_RULE_IDS)

def build_mihomo_sync_rule_lines(proxy_policy, item_indent=""):
    return [
        f"{item_indent}{RULE_SYNC_BEGIN}\n",
        f"{item_indent}- RULE-SET,{RULE_PROVIDER_NAMES['force-cn']},DIRECT\n",
        f"{item_indent}- RULE-SET,{RULE_PROVIDER_NAMES['force-nocn']},{proxy_policy}\n",
        f"{item_indent}{RULE_SYNC_END}\n",
    ]

def build_fake_ip_filter_lines(item_indent):
    # 只有强制直连需要拿真实 IP，强制代理继续走 fake-ip
    return [
        f"{item_indent}{FAKE_IP_FILTER_BEGIN}\n",
        f"{item_indent}- rule-set:{RULE_PROVIDER_NAMES['force-cn']}\n",
        f"{item_indent}{FAKE_IP_FILTER_END}\n",
    ]

def build_rule_provider_lines(child_indent):
    block = [f"{child_indent}{RULE_PROVIDERS_BEGIN}\n"]
    for rule_id, name in RULE_PROVIDER_NAMES.items():
        relative = "./rules/" + os.path.basename(RULE_PROVIDER_FILES[rule_id])
        block.append(f"{child_indent}{name}: {{type: file, behavior: domain, format: yaml, path: {relative}}}\n")
    block.append(f"{child_indent}{RULE_PROVIDERS_END}\n")
    return block

def remove_marker_block(lines, begin, end_marker):
    start = next((idx for idx, line in enumerate(lines) if line.strip() == begin), -1)
    if start < 0:
        return lines
    end = next((idx for idx in range(start + 1, len(lines)) if lines[idx].strip() == end_marker), start)
    return lines[:start] + lines[end + 1:]

def remove_fake_ip_filter_block(lines):
    return remove_marker_block(lines, FAKE_IP_FILTER_BEGIN, FAKE_IP_FILTER_END)

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

def mapping_child_indent_after_key(lines, key_index):
    """映射（如 rule-providers:）下子键的缩进：沿用第一个子项的缩进，没有子项时用两格。"""
    key_indent = line_indent(lines[key_index])
    for line in lines[key_index + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = line_indent(line)
        if len(indent) > len(key_indent):
            return indent
        break
    return key_indent + "  "

def update_fake_ip_filter_block(lines):
    lines = remove_fake_ip_filter_block(lines)
    dns_index = find_yaml_top_level_key(lines, "dns")
    if dns_index < 0:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("dns:\n")
        lines.append("  fake-ip-filter:\n")
        lines.extend(build_fake_ip_filter_lines("    "))
        return lines

    fake_filter_index = find_nested_key(lines, dns_index, "fake-ip-filter")
    if fake_filter_index < 0:
        dns_indent = line_indent(lines[dns_index])
        child_indent = dns_indent + "  "
        item_indent = child_indent + "  "
        insert_at = section_end_index(lines, dns_index)
        lines[insert_at:insert_at] = [f"{child_indent}fake-ip-filter:\n"] + build_fake_ip_filter_lines(item_indent)
        return lines

    item_indent = list_item_indent_after_key(lines, fake_filter_index)
    lines[fake_filter_index + 1:fake_filter_index + 1] = build_fake_ip_filter_lines(item_indent)
    return lines

def update_rule_providers_block(lines):
    lines = remove_marker_block(lines, RULE_PROVIDERS_BEGIN, RULE_PROVIDERS_END)
    # rule-providers: {} / null 这种空值写法改成块映射，才能往下插子项
    lines = [
        "rule-providers:\n" if re.match(r"^rule-providers\s*:\s*(?:\{\s*\}|null|~)\s*(?:#.*)?$", line.rstrip("\n")) else line
        for line in lines
    ]
    providers_index = find_yaml_top_level_key(lines, "rule-providers")
    if providers_index < 0:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("rule-providers:\n")
        lines.extend(build_rule_provider_lines("  "))
        return lines
    child_indent = mapping_child_indent_after_key(lines, providers_index)
    lines[providers_index + 1:providers_index + 1] = build_rule_provider_lines(child_indent)
    return lines

def render_sync_blocks(text):
    """写入规则集格式的三个标记块（rule-providers / rules / fake-ip-filter）。
    这些块只引用规则集名字，不含域名，写好后内容固定；旧格式的内联域名会被替换掉。"""
    lines = text.splitlines(True)
    if not lines:
        lines = ["rules:\n"]
    proxy_policy = proxy_policy_name(text)

    start = next((idx for idx, line in enumerate(lines) if line.strip() == RULE_SYNC_BEGIN), -1)
    if start >= 0:
        end = next((idx for idx in range(start + 1, len(lines)) if lines[idx].strip() == RULE_SYNC_END), start)
        # 沿用已有标记行的缩进
        block = build_mihomo_sync_rule_lines(proxy_policy, line_indent(lines[start]))
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
        block = build_mihomo_sync_rule_lines(proxy_policy, item_indent)
        lines[rules_index + 1:rules_index + 1] = block
    lines = update_fake_ip_filter_block(lines)
    lines = update_rule_providers_block(lines)
    return "".join(lines)


def reapply_sync_blocks(source_path, target_path):
    """把同步规则带到重新生成的 target 配置里。订阅更新会重新生成 config.yaml，
    不做这一步，面板里保存的强制直连/强制代理规则每次更新都会丢。
    域名在规则文件里，这里只注入引用规则集的标记块；source 还是旧的内联格式时顺便把域名迁到规则文件。"""
    with open(source_path, "r", encoding="utf-8") as f:
        source_text = f.read()
    mode = sync_blocks_mode(source_text)
    if mode == "none":
        return False
    if mode == "legacy":
        write_rule_provider_files(read_legacy_inline_sync_rules(source_text))
    with open(target_path, "r", encoding="utf-8") as f:
        target_text = f.read()
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(render_sync_blocks(target_text))
    return True


def refresh_rule_providers():
    """通知运行中的 mihomo 重新读规则文件（PUT /providers/rules/<name>），规则和 fake-ip-filter 都会生效，不用重启。"""
    settings = mihomo_controller_settings()
    headers = {"User-Agent": "mihomo-web-manager"}
    if settings.get("secret"):
        headers["Authorization"] = "Bearer " + settings["secret"]
    for name in RULE_PROVIDER_NAMES.values():
        url = settings["base_url"] + "/providers/rules/" + quote(name)
        try:
            req = urlrequest.Request(url, data=b"", headers=headers, method="PUT")
            with urlrequest.urlopen(req, timeout=5) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
        except Exception as e:
            return False, f"{name}: {e}"
        if status not in (200, 204):
            return False, f"{name}: HTTP {status}"
    return True, "ok"

def sync_load_mode_label(text=None):
    if text is None:
        text = read_config_text()
    if sync_blocks_mode(text) == "provider":
        return "规则集热加载（无需重启）"
    return "旧格式（下次保存时自动升级，需重启一次）"

def update_mihomo_sync_block(rule_contents):
    """让新的同步规则生效。返回 (ok, 说明, 后续动作)，动作是 reloaded（已热加载）或 restart（调用方要重启 mihomo）。

    config.yaml 已是规则集格式：只写规则文件再热刷新；刷新失败退回重启。
    还是旧格式/没有标记块：一次性改写 config.yaml（校验通过才落盘），需要重启一次。"""
    if not CONFIG_LOCK.acquire(blocking=False):
        return False, BUSY_MESSAGE, None
    try:
        text = read_config_text()
        new_text = render_sync_blocks(text)
        if new_text == text:
            write_rule_provider_files(rule_contents)
            ok, detail = refresh_rule_providers()
            if ok:
                return True, "规则已热加载，无需重启 mihomo", "reloaded"
            return True, f"规则集热加载失败（{detail}），改为重启 mihomo", "restart"

        upgrading = sync_blocks_mode(text) == "legacy"
        snapshot = snapshot_rule_provider_files()
        write_rule_provider_files(rule_contents)
        tmp_file = write_tmp_config(new_text, ".rulesync")
        try:
            ok, message = validate_config(tmp_file)
        finally:
            remove_quietly(tmp_file)
        if not ok:
            restore_rule_provider_files(snapshot)
            return False, "规则写入后配置校验失败，已取消保存：\n" + message, None
        write_config_text(new_text)
        # 这次改写只动 rules / rule-providers / dns.fake-ip-filter，不涉及网络结构，热加载整个配置即可；
        # 热加载失败才交给调用方重启（收到同步时要先回响应再在后台重启）
        reloaded, detail = hot_reload_mihomo()
        if reloaded:
            if upgrading:
                return True, "同步规则已一次性升级为规则集格式并热加载，未中断连接", "reloaded"
            return True, "已写入规则集配置并热加载，未中断连接", "reloaded"
        if upgrading:
            return True, f"同步规则已一次性升级为规则集格式（热加载失败：{detail}，改为重启 mihomo）", "restart"
        return True, "已写入规则集配置（本次需重启 mihomo）", "restart"
    finally:
        CONFIG_LOCK.release()

def save_rule_content(rule_id, content):
    """返回 (ok, 说明, 动作)；动作为 unchanged / reloaded / restart，见 update_mihomo_sync_block。"""
    if rule_id not in SYNCABLE_RULE_IDS:
        return False, "未知规则文件", None
    if not is_safe_text(content):
        return False, "规则内容不合法或过大", None
    problem, note = rule_content_problem(content)
    if problem:
        return False, problem + "，未保存", None
    current = read_mihomo_sync_rules()
    new_contents = dict(current)
    new_contents[rule_id] = rule_content_text(normalize_rule_domains(content))
    if sync_rules_unchanged(current, new_contents):
        return True, RULES_UNCHANGED_MESSAGE + (f"（{note}）" if note else ""), "unchanged"
    ok, message, action = update_mihomo_sync_block(new_contents)
    return ok, message + (f"（{note}）" if ok and note else ""), action

def restart_mihomo():
    return run_args(["systemctl", "restart", "mihomo"], timeout=60)

def schedule_mihomo_restart():
    subprocess.Popen(
        ["sh", "-c", "sleep 1; systemctl restart mihomo"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

# 改了这些键需要重启：TUN、监听端口/地址、控制器、出站接口、DNS 监听方式。
# 其余（proxies / proxy-groups / rules / rule-providers / log-level / dns.nameserver / fake-ip-filter 等）
# 用控制器 PUT /configs 热加载即可，进程和已有连接都不受影响（v1.19.32 实测）。
RESTART_TOP_LEVEL_KEYS = (
    "tun", "port", "socks-port", "mixed-port", "redir-port", "tproxy-port", "allow-lan", "bind-address",
    "ipv6", "listeners", "external-controller", "external-controller-tls", "external-ui", "secret",
    "interface-name", "routing-mark", "profile",
)
RESTART_DNS_KEYS = ("listen", "enable", "enhanced-mode", "fake-ip-range")
CONFIG_COMPARE_FAILED_REASON = "无法比较配置，保守起见重启"
HOT_RELOAD_TIMEOUT = 30

def config_needs_restart(old_text, new_text):
    """返回 (是否需要重启, 原因列表)。两边都能解析成映射才比较，否则保守重启。"""
    if yaml is None:
        return True, [CONFIG_COMPARE_FAILED_REASON]
    try:
        old = yaml.safe_load(old_text or "")
        new = yaml.safe_load(new_text or "")
    except Exception:
        return True, [CONFIG_COMPARE_FAILED_REASON]
    if not isinstance(old, dict) or not isinstance(new, dict):
        return True, [CONFIG_COMPARE_FAILED_REASON]
    reasons = [f"修改了 {key}" for key in RESTART_TOP_LEVEL_KEYS if old.get(key) != new.get(key)]
    old_dns = old.get("dns") if isinstance(old.get("dns"), dict) else {}
    new_dns = new.get("dns") if isinstance(new.get("dns"), dict) else {}
    reasons += [f"修改了 dns.{key}" for key in RESTART_DNS_KEYS if old_dns.get(key) != new_dns.get(key)]
    return bool(reasons), reasons

def hot_reload_mihomo(path=None):
    """让运行中的 mihomo 重新读配置文件（PUT /configs?force=true），不重启进程、不断开已有连接。
    path 默认是 CONFIG_FILE。"""
    path = path or CONFIG_FILE
    settings = mihomo_controller_settings()
    headers = {"User-Agent": "mihomo-web-manager", "Content-Type": "application/json"}
    if settings.get("secret"):
        headers["Authorization"] = "Bearer " + settings["secret"]
    body = json.dumps({"path": path, "payload": ""}).encode("utf-8")
    try:
        req = urlrequest.Request(settings["base_url"] + "/configs?force=true", data=body, headers=headers, method="PUT")
        with urlrequest.urlopen(req, timeout=HOT_RELOAD_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
    except urlerror.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        return False, f"HTTP {e.code}" + (f"：{detail[:200]}" if detail else "")
    except Exception as e:
        return False, str(e)
    if status not in (200, 204):
        return False, f"HTTP {status}"
    ok, info = mihomo_api_get("/version", timeout=5)
    if not ok:
        return False, "热加载后控制器无响应：" + str(info.get("error") if isinstance(info, dict) else info)
    return True, "ok"

def apply_config_change(old_text, new_text):
    """config.yaml 已写成 new_text 之后让它生效。返回 (ok, 说明, 动作)，动作为 unchanged / reloaded / restarted。

    没碰网络结构的改动走热加载；改了 TUN/端口等、或无法比较时重启；热加载失败退回重启。"""
    if config_text_key(old_text) == config_text_key(new_text):
        return True, CONFIG_UNCHANGED_MESSAGE, "unchanged"
    needs_restart, reasons = config_needs_restart(old_text, new_text)
    if needs_restart:
        ok, output = restart_mihomo()
        if ok:
            return True, "已重启（因为" + "，".join(reasons) + "）", "restarted"
        return False, "mihomo 重启失败（" + "，".join(reasons) + "）：\n" + output, "restarted"
    reloaded, detail = hot_reload_mihomo()
    if reloaded:
        return True, "已热加载，未中断连接", "reloaded"
    ok, output = restart_mihomo()
    if ok:
        return True, f"热加载失败，已改为重启：{detail}", "restarted"
    return False, f"热加载失败，改为重启也失败：{detail}\n" + output, "restarted"


def local_ipv4_addresses():
    """本机所有 IPv4 地址（含回环），用来识别同步节点里的“自己”。"""
    addrs = {"127.0.0.1", "localhost"}
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5).stdout
        addrs.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out))
    except Exception:
        pass
    return addrs

def is_self_peer(peer, own_port):
    """同步节点列表在各处是同一份，会包含本机。推送给自己会撞上本机正持有的锁（409），只会显示成失败。"""
    try:
        parts = urlsplit(peer)
        host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    try:
        own_port = int(own_port)
    except (TypeError, ValueError):
        return False
    return port == own_port and host in local_ipv4_addresses()

# 规则同步在后台线程里推送：保存规则的请求不再等各节点返回。
# 节点里可能有本机上游的 mihomo，它重启时会断掉浏览器经由它的连接，同步等在请求里就会让页面误报“请求中断”。
SYNC_JOBS = []
SYNC_JOBS_LOCK = threading.Lock()
SYNC_JOBS_KEEP = 5
SYNC_PEER_TIMEOUT = 15
SYNC_BUSY_RETRIES = 3
SYNC_BUSY_DELAY = 3
SYNC_SELF_MESSAGE = "本机（跳过）"
# 对端忙时的提示：mosctl 返回 409“操作进行中，请稍后再试”，mihomo 返回 200 + “另一个操作正在进行中，请稍后再试。”
SYNC_BUSY_HINTS = ("稍后再试", "操作进行中", "正在进行中")


def sync_peer_busy(message):
    return any(hint in str(message or "") for hint in SYNC_BUSY_HINTS)


def push_rule_to_peer(peer, payload, headers):
    """推给一个节点，返回 (成功, 说明)。对端忙（409 或忙碌提示）时隔 SYNC_BUSY_DELAY 秒重试，最多 SYNC_BUSY_RETRIES 次。"""
    url = peer.rstrip("/") + "/api/rule-sync"
    attempt = 0
    while True:
        busy = False
        try:
            req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
            with urlrequest.urlopen(req, timeout=SYNC_PEER_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(body, dict):
                body = {}
            if body.get("success"):
                return True, "成功"
            message = str(body.get("message") or "未知错误")
            busy = sync_peer_busy(message)
        except urlerror.HTTPError as exc:
            message = str(exc)
            busy = exc.code == 409
            exc.close()
        except Exception as exc:
            message = str(exc)
        if not busy or attempt >= SYNC_BUSY_RETRIES:
            if busy and attempt:
                message += f"（已重试 {attempt} 次）"
            return False, message
        attempt += 1
        time.sleep(SYNC_BUSY_DELAY)


def sync_job_snapshot(job):
    with SYNC_JOBS_LOCK:
        return json.loads(json.dumps(job))


def find_sync_job(job_id):
    with SYNC_JOBS_LOCK:
        if job_id == "latest":
            job = SYNC_JOBS[-1] if SYNC_JOBS else None
        else:
            job = next((item for item in SYNC_JOBS if item["id"] == job_id), None)
    return sync_job_snapshot(job) if job else None


def run_broadcast_job(job, peers, payload, headers):
    for peer in peers:
        try:
            ok, message = push_rule_to_peer(peer, payload, headers)
        except Exception as exc:  # 线程里不能把异常抛丢，任务要能结束
            ok, message = False, str(exc)
        with SYNC_JOBS_LOCK:
            job["results"].append({"peer": peer, "success": ok, "message": message})
            job["done"] += 1
    with SYNC_JOBS_LOCK:
        job["finished_at"] = int(time.time())


def start_broadcast(rule_id, content):
    """在后台推送规则，返回 (任务 id 或 None, 给用户的说明)。

    依赖请求的东西（source、设置、本机端口、本机识别）都在这里取好再起线程；线程不持有操作锁。
    """
    if rule_id not in SYNCABLE_RULE_IDS:
        return None, ""
    settings = read_sync_settings()
    if not settings["enabled"]:
        return None, "规则同步未启用。"
    if not settings["peers"]:
        return None, "规则同步已启用，但没有配置其他节点。"
    if not settings["token"]:
        return None, "规则同步已启用，但缺少同步密钥。"

    payload = json.dumps(
        {
            "token": settings["token"],
            "rules": {rule_id: content},
            "source": request.host_url.rstrip("/"),
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Mosdns-Sync-Token": settings["token"],
    }
    own_port = read_env().get("WEB_PORT", "7838")
    remote_peers = []
    results = []
    for peer in settings["peers"]:
        if is_self_peer(peer, own_port):
            results.append({"peer": peer, "success": True, "message": SYNC_SELF_MESSAGE})
        else:
            remote_peers.append(peer)
    job = {
        "id": secrets.token_hex(4),
        "rule_id": rule_id,
        "started_at": int(time.time()),
        "finished_at": None,
        "total": len(settings["peers"]),
        "done": len(results),
        "results": results,
    }
    with SYNC_JOBS_LOCK:
        SYNC_JOBS.append(job)
        del SYNC_JOBS[:-SYNC_JOBS_KEEP]
    threading.Thread(
        target=run_broadcast_job,
        args=(job, remote_peers, payload, headers),
        name="rule-sync-" + job["id"],
        daemon=True,
    ).start()
    return job["id"], f"正在后台同步到 {len(remote_peers)} 个节点…"


def apply_synced_rules(rules):
    if not isinstance(rules, dict):
        return False, "同步内容不合法"
    if not CONFIG_LOCK.acquire(blocking=False):
        return False, BUSY_MESSAGE
    try:
        current = read_mihomo_sync_rules()
        new_contents = dict(current)
        applied = []
        for rule_id, content in rules.items():
            if rule_id not in SYNCABLE_RULE_IDS:
                continue
            if not is_safe_text(content):
                return False, "规则内容不合法或过大"
            new_contents[rule_id] = rule_content_text(normalize_rule_domains(content))
            applied.append(rule_id)
        if not applied:
            return False, "没有可同步的规则"
        if sync_rules_unchanged(current, new_contents):
            return True, RULES_UNCHANGED_MESSAGE
        ok, message, action = update_mihomo_sync_block(new_contents)
        if not ok:
            return False, message
        prefix = "已同步规则：" + ", ".join(applied) + "，"
        if action == "restart":
            # 先回 HTTP 响应再重启：对端可能正经由这台 mihomo 访问本接口
            schedule_mihomo_restart()
            return True, prefix + message + "，mihomo 将在后台重启"
        return True, prefix + message
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
    own_port = read_env().get("WEB_PORT", "7838")
    for peer in peers:
        if is_self_peer(peer, own_port):
            results.append({"peer": peer, "success": True, "message": "本机（跳过）"})
            continue
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
    """最近一次测速结果：毫秒数；0 表示最近一次超时；没有记录返回 None。"""
    history = proxy.get("history") if isinstance(proxy, dict) else None
    if not isinstance(history, list) or not history:
        return None
    delay = history[-1].get("delay") if isinstance(history[-1], dict) else None
    return delay if isinstance(delay, (int, float)) and delay >= 0 else None

def resolve_group_leaf(items, name, limit=10):
    """顺着分组的当前选择一路往下找，返回最终实际使用的节点名（分组本身不测速，延迟要看这个节点）。"""
    seen = set()
    current = name
    while limit > 0:
        proxy = items.get(current)
        if not isinstance(proxy, dict) or "all" not in proxy or not proxy.get("now") or current in seen:
            return current
        seen.add(current)
        current = proxy["now"]
        limit -= 1
    return current

def proxy_group_summary(proxies):
    items = proxies.get("proxies") if isinstance(proxies, dict) else {}
    if not isinstance(items, dict):
        return []
    # GLOBAL 的 all 就是配置文件里的顺序；按它排，GLOBAL 自己不显示
    global_group = items.get("GLOBAL") if isinstance(items.get("GLOBAL"), dict) else {}
    order = {n: i for i, n in enumerate(global_group.get("all") or [])}
    names = [n for n, p in items.items() if n != "GLOBAL" and isinstance(p, dict) and "all" in p]
    names.sort(key=lambda n: order.get(n, len(order)))
    groups = []
    for name in names:
        proxy = items[name]
        leaf = resolve_group_leaf(items, name)
        groups.append({
            "name": name,
            "type": proxy.get("type", "Group"),
            "now": proxy.get("now", ""),
            "leaf": leaf if leaf not in (name, proxy.get("now")) else "",
            "count": len(proxy.get("all") or []),
            "delay": latest_delay(items.get(leaf)),
        })
    return groups

# 概览页的日志统计：只看最近 1 小时（按日志里的时间戳算），附最近几条警告/错误。
# 概览每 5 秒刷新一次，结果缓存 30 秒，日志没变也直接复用
LOG_SUMMARY_WINDOW = 3600
LOG_SUMMARY_TAIL_BYTES = 4 * 1024 * 1024
LOG_SUMMARY_CACHE_SECONDS = 30
LOG_SUMMARY_RECENT = 5
LOG_SUMMARY_CACHE = {}
LOG_LINE_RE = re.compile(r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})[^"]*Z?" level=([a-zA-Z]+) msg="(.*)"\s*$')

def parse_log_epoch(stamp):
    try:
        return int(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return None

def log_level_summary(now=None):
    now = time.time() if now is None else now
    try:
        stat = os.stat(LOG_FILE)
    except OSError:
        return {"error": 0, "warn": 0, "info": 0, "debug": 0, "window": LOG_SUMMARY_WINDOW, "recent": []}
    key = (stat.st_size, stat.st_mtime)
    cached = LOG_SUMMARY_CACHE.get("value")
    if cached and LOG_SUMMARY_CACHE.get("key") == key and now - LOG_SUMMARY_CACHE.get("at", 0) < LOG_SUMMARY_CACHE_SECONDS:
        return cached
    levels = {"error": 0, "warn": 0, "info": 0, "debug": 0}
    recent = []
    since = now - LOG_SUMMARY_WINDOW
    text = read_recent_log_lines(LOG_FILE, 100000, tail_bytes=LOG_SUMMARY_TAIL_BYTES)
    for line in text.splitlines():
        match = LOG_LINE_RE.match(line)
        if not match:
            continue
        at = parse_log_epoch(match.group(1))
        if at is None or at < since:
            continue
        level = match.group(2).lower()
        level = "warn" if level == "warning" else level
        if level not in levels:
            continue
        levels[level] += 1
        if level in ("warn", "error"):
            recent.append({"at": at, "level": level, "message": match.group(3).replace('\\"', '"')[:300]})
    value = dict(levels, window=LOG_SUMMARY_WINDOW, recent=recent[-LOG_SUMMARY_RECENT:][::-1])
    LOG_SUMMARY_CACHE.update({"key": key, "at": now, "value": value})
    return value

def subscription_count(env):
    raw = [env.get("SUB_URL_RAW", "")]
    airport = str(env.get("SUB_URL_AIRPORT", "")).replace("\\n", "\n").splitlines()
    return len([item for item in raw + airport if item.strip()])

def last_subscription_state(path=None):
    """读取订阅脚本上次运行的结果；没跑过返回 None。"""
    path = path or SUBSCRIPTION_STATE_FILE
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            line = f.readline().rstrip("\n")
    except OSError:
        return None
    parts = line.split("\t", 2)
    if len(parts) < 2:
        return None
    try:
        at = int(float(parts[0]))
    except ValueError:
        return None
    return {"at": at, "ok": parts[1] == "ok", "message": parts[2] if len(parts) > 2 else ""}

# ---------------------------------------------------------------------------
# 设备流量统计
#
# 这台机器是网关（TUN + fake-ip），mihomo 控制器的 /connections 能看到局域网里每台设备的连接；
# mosdns 面板只看得到 DNS，看不到流量，所以设备视图放在这里。后台线程每 DEVICE_SAMPLE_INTERVAL 秒
# 读一次连接列表，按 sourceIP 累加每条连接相对上次采样的字节增量，并定期写到 devices.json。
# ---------------------------------------------------------------------------
DEVICES_FILE = f"{MIHOMO_DIR}/devices.json"
DEVICE_SAMPLE_INTERVAL = 5
DEVICE_ONLINE_SECONDS = 90
DEVICE_PERSIST_INTERVAL = 30
DEVICE_NEIGH_INTERVAL = 30
DEVICE_MAX_DOMAINS = 200
DEVICE_MAX_CHAINS = 50
DEVICE_MAX_DEVICES = 500
DEVICE_NOTE_MAX_LEN = 80
# 网关自己（127.0.0.1、本机地址）发出的连接归到一个伪设备下，mosdns 转发过来的 DNS 流量才有地方看
GATEWAY_DEVICE_KEY = "本机/网关"
# 公网来源（比如通过 ss 入站连进来的远程客户端）统一归到一个伪设备下，不然每个漫游 IP 都成一台"设备"
REMOTE_DEVICE_KEY = "远程客户端"
DEVICE_REMOTE_IPS_KEEP = 100      # 状态里保留的远程来源 IP 数
DEVICE_REMOTE_IPS_SHOW = 10       # 接口里展示的最近来源 IP 数
LOOPBACK_IPS = frozenset({"127.0.0.1", "::1"})
# RFC 6598 运营商级 NAT 段（Tailscale 等也用它），按内网算
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# ip neigh 里这些状态的邻居才算"局域网可见"；FAILED / INCOMPLETE 是解析失败的残留
NEIGHBOUR_VISIBLE_STATES = frozenset({"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"})

DEVICE_LOCK = threading.Lock()
DEVICE_STATE = {
    "devices": {},            # ip -> 累计数据（持久化）
    "notes": {},              # ip -> 备注（持久化）
    "prev": {},               # 连接 id -> (upload, download) 上次采样值，用来算增量（不持久化）
    "sampled_at": 0,          # 上次成功采样的时间
    "controller_error": "",
    "controller_error_at": 0,
    "local_ips": set(LOOPBACK_IPS),
    "macs": {},
    "neighbours": {},         # ip -> {"mac", "state"}，来自 ip -4 neigh（不持久化）
    "upstream_router": "",    # ip route 里 default via 的下一跳
    "dirty": False,           # 有未落盘的改动
}
DEVICE_SAMPLER_STARTED = False
DEVICE_SAMPLER_STOP = threading.Event()

def normalize_source_ip(value):
    """控制器对 IPv4 来源常给 ::ffff:a.b.c.d 这种映射地址，统一成 a.b.c.d。"""
    ip = str(value or "").strip()
    if ip.startswith("[") and ip.endswith("]"):
        ip = ip[1:-1]
    if ip.lower().startswith("::ffff:") and ip.count(".") == 3:
        ip = ip[7:]
    return ip

def detect_local_ips():
    ips = set(LOOPBACK_IPS)
    ok, output = run_args(["ip", "-4", "-o", "addr"], timeout=5)
    if ok:
        ips.update(re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", output))
    return ips

NEIGH_LINE_RE = re.compile(r"^(\S+)\s+dev\s+\S+(?:\s+lladdr\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}))?.*?\s([A-Z]+)\s*$")

def parse_neighbours(output):
    """ip neigh 的每一行：<ip> dev <if> [lladdr <mac>] [flags] <STATE>。"""
    neighbours = {}
    for line in str(output or "").splitlines():
        match = NEIGH_LINE_RE.match(line.strip())
        if match:
            ip, mac, state = match.groups()
            neighbours[ip] = {"mac": (mac or "").lower(), "state": state}
    return neighbours

def detect_neighbours():
    """ip -4 neigh 里的直连邻居。MAC 只用来显示，状态只用来把"同网段可见但没流量"的设备列出来，绝不拿它判断在线。"""
    ok, output = run_args(["ip", "-4", "neigh"], timeout=5)
    return parse_neighbours(output) if ok else {}

def detect_neighbour_macs():
    return {ip: info["mac"] for ip, info in detect_neighbours().items() if info["mac"]}

def detect_upstream_router():
    """默认路由的下一跳。它做了 NAT 再转发的流量都顶着它自己的 IP，面板里要标出来"可能是多台设备"。"""
    ok, output = run_args(["ip", "route"], timeout=5)
    if not ok:
        return ""
    match = re.search(r"^default\s+via\s+(\S+)", output, re.M)
    return match.group(1) if match else ""

def source_kind(ip, local_ips):
    """gateway：网关自己；lan：内网 / 链路本地 / CGNAT 来源；remote：公网来源。解析不了的地址按 lan 处理。"""
    if ip in local_ips:
        return "gateway"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "lan"
    if addr.is_loopback:
        return "gateway"
    if addr.is_private or addr.is_link_local or (addr.version == 4 and addr in CGNAT_NETWORK):
        return "lan"
    return "remote"

def ip_sort_key(ip):
    try:
        addr = ipaddress.ip_address(ip)
        return (addr.version, int(addr))
    except ValueError:
        return (9, 0)

def new_device_record(key, now, kind="lan"):
    return {
        "ip": key,
        "kind": kind,                 # lan / remote / gateway
        "is_gateway": kind == "gateway",
        "mac": "",
        "inbounds": {},               # 入站类型（Tun / ShadowSocks / ...）-> 见过的连接数
        "remote_ips": {},             # 仅远程伪设备：来源 IP -> 最近出现时间
        "first_seen": int(now),
        "last_seen": int(now),
        "upload_total": 0,
        "download_total": 0,
        "domains": {},            # host -> {count, bytes, last}
        "chains": {},             # 出口链路标签 -> bytes
        "active_connections": 0,
        "rate_up": 0,
        "rate_down": 0,
        "last_host": "",
        "last_start": "",
    }

def connection_chain_label(conn):
    """mihomo 的 chains 是出口节点在前、规则命中的策略组在后；显示成 策略组 → 节点，直连就是 DIRECT。"""
    chains = conn.get("chains") if isinstance(conn, dict) else None
    names = [str(item).strip() for item in chains if str(item or "").strip()] if isinstance(chains, list) else []
    if not names:
        return "DIRECT"
    return " → ".join(reversed(names)) if len(names) > 1 else names[0]

def trim_device_record(device):
    """域名表最多 DEVICE_MAX_DOMAINS 条（淘汰最久没访问的），链路表最多 DEVICE_MAX_CHAINS 条。"""
    domains = device.get("domains") or {}
    if len(domains) > DEVICE_MAX_DOMAINS:
        for host, _ in sorted(domains.items(), key=lambda item: item[1].get("last", 0))[: len(domains) - DEVICE_MAX_DOMAINS]:
            domains.pop(host, None)
    chains = device.get("chains") or {}
    if len(chains) > DEVICE_MAX_CHAINS:
        for label, _ in sorted(chains.items(), key=lambda item: item[1])[: len(chains) - DEVICE_MAX_CHAINS]:
            chains.pop(label, None)
    remote_ips = device.get("remote_ips") or {}
    if len(remote_ips) > DEVICE_REMOTE_IPS_KEEP:
        for ip, _ in sorted(remote_ips.items(), key=lambda item: item[1])[: len(remote_ips) - DEVICE_REMOTE_IPS_KEEP]:
            remote_ips.pop(ip, None)

def trim_device_table(devices):
    """设备超过 DEVICE_MAX_DEVICES 台时淘汰最久没活动的，网关 / 远程客户端伪设备不淘汰。"""
    if len(devices) <= DEVICE_MAX_DEVICES:
        return
    candidates = sorted(
        (key for key, device in devices.items() if device.get("kind", "lan") == "lan"),
        key=lambda key: devices[key].get("last_seen", 0),
    )
    for key in candidates[: len(devices) - DEVICE_MAX_DEVICES]:
        devices.pop(key, None)

def apply_connections_sample(connections, now=None, local_ips=None):
    """把一次 /connections 快照累加进 DEVICE_STATE，返回本次有连接的设备数。

    增量规则：新 id 按当前值全额计入；见过的 id 按 当前 - 上次 计入；计数器变小说明 id 被复用，
    按新连接处理。上次有、这次没有的 id 直接丢掉，它之前累加过的字节保留。
    """
    now = int(now if now is not None else time.time())
    with DEVICE_LOCK:
        if local_ips is None:
            local_ips = DEVICE_STATE["local_ips"]
        devices = DEVICE_STATE["devices"]
        prev = DEVICE_STATE["prev"]
        last_at = DEVICE_STATE["sampled_at"]
        elapsed = now - last_at
        interval = elapsed if last_at and 0 < elapsed <= DEVICE_SAMPLE_INTERVAL * 12 else DEVICE_SAMPLE_INTERVAL
        seen = {}
        deltas = {}   # key -> [up, down, active]
        for conn in connections if isinstance(connections, list) else []:
            if not isinstance(conn, dict):
                continue
            meta = conn.get("metadata") if isinstance(conn.get("metadata"), dict) else {}
            src = normalize_source_ip(meta.get("sourceIP"))
            if not src:
                continue
            cid = str(conn.get("id") or "")
            up, down = first_number(conn.get("upload")), first_number(conn.get("download"))
            is_new = not cid or cid not in prev
            if is_new:
                du, dd = up, down
            else:
                du, dd = up - prev[cid][0], down - prev[cid][1]
                if du < 0 or dd < 0:
                    du, dd, is_new = up, down, True
            if cid:
                seen[cid] = (up, down)
            kind = source_kind(src, local_ips)
            key = {"gateway": GATEWAY_DEVICE_KEY, "remote": REMOTE_DEVICE_KEY}.get(kind, src)
            device = devices.get(key)
            if device is None:
                device = devices[key] = new_device_record(key, now, kind)
            if kind == "remote":
                device["remote_ips"][src] = now
            # metadata.type 是入站类型：Tun 是局域网走网关的流量，ShadowSocks / HTTPS / Socks5 / Mixed 是代理端口
            inbound = str(meta.get("type") or "").strip() or "Unknown"
            if is_new:
                device["inbounds"][inbound] = device["inbounds"].get(inbound, 0) + 1
            device["upload_total"] += du
            device["download_total"] += dd
            device["last_seen"] = now
            delta = deltas.setdefault(key, [0, 0, 0])
            delta[0] += du
            delta[1] += dd
            delta[2] += 1
            host = str(meta.get("host") or meta.get("destinationIP") or "").strip()[:253]
            if host:
                entry = device["domains"].get(host)
                if entry is None:
                    entry = device["domains"][host] = {"count": 0, "bytes": 0, "last": now}
                entry["bytes"] += du + dd
                entry["last"] = now
                if is_new:
                    entry["count"] += 1
                start = str(conn.get("start") or "")
                if not device.get("last_host") or start >= device.get("last_start", ""):
                    device["last_host"], device["last_start"] = host, start
            label = connection_chain_label(conn)
            device["chains"][label] = device["chains"].get(label, 0) + du + dd
        for key, device in devices.items():
            delta = deltas.get(key)
            device["active_connections"] = delta[2] if delta else 0
            device["rate_up"] = int(delta[0] / interval) if delta else 0
            device["rate_down"] = int(delta[1] / interval) if delta else 0
            if delta:
                trim_device_record(device)
        trim_device_table(devices)
        DEVICE_STATE["prev"] = seen
        DEVICE_STATE["sampled_at"] = now
        DEVICE_STATE["controller_error"] = ""
        DEVICE_STATE["dirty"] = True
        return len(deltas)

def coerce_device_record(key, raw):
    """devices.json 里的一条设备记录，字段逐个校验类型，坏文件不能把面板搞挂。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "")
    if kind not in ("lan", "remote", "gateway"):
        # 0.1.28 之前的 devices.json 没有 kind：公网来源的旧记录要按 remote 处理，不能当局域网设备
        if raw.get("is_gateway"):
            kind = "gateway"
        elif key == REMOTE_DEVICE_KEY:
            kind = "remote"
        else:
            try:
                kind = source_kind(key)
            except Exception:
                kind = "lan"
    record = new_device_record(key, first_number(raw.get("first_seen")), kind)
    record["last_seen"] = first_number(raw.get("last_seen"))
    record["upload_total"] = max(0, first_number(raw.get("upload_total")))
    record["download_total"] = max(0, first_number(raw.get("download_total")))
    record["mac"] = str(raw.get("mac") or "")[:32]
    record["last_host"] = str(raw.get("last_host") or "")[:253]
    record["last_start"] = str(raw.get("last_start") or "")[:64]
    domains = raw.get("domains") if isinstance(raw.get("domains"), dict) else {}
    for host, entry in domains.items():
        if isinstance(entry, dict) and isinstance(host, str) and host:
            record["domains"][host[:253]] = {
                "count": max(0, first_number(entry.get("count"))),
                "bytes": max(0, first_number(entry.get("bytes"))),
                "last": first_number(entry.get("last")),
            }
    chains = raw.get("chains") if isinstance(raw.get("chains"), dict) else {}
    for label, total in chains.items():
        if isinstance(label, str) and label:
            record["chains"][label[:200]] = max(0, first_number(total))
    inbounds = raw.get("inbounds") if isinstance(raw.get("inbounds"), dict) else {}
    for name, count in inbounds.items():
        if isinstance(name, str) and name:
            record["inbounds"][name[:40]] = max(0, first_number(count))
    remote_ips = raw.get("remote_ips") if isinstance(raw.get("remote_ips"), dict) else {}
    for ip, last in remote_ips.items():
        if isinstance(ip, str) and ip:
            record["remote_ips"][ip[:64]] = first_number(last)
    trim_device_record(record)
    return record

def load_device_state(path=None):
    """启动时读回上次落盘的累计值；文件缺失或损坏就从空表开始。"""
    path = path or DEVICES_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    devices = {}
    raw_devices = data.get("devices") if isinstance(data.get("devices"), dict) else {}
    for key, raw in raw_devices.items():
        # 旧版本把公网来源按单个 IP 存过；它们属于“远程客户端”汇总，丢掉旧记录，采样时会重新归并
        try:
            if str(key) != REMOTE_DEVICE_KEY and not raw.get("is_gateway") and source_kind(str(key)) == "remote":
                continue
        except Exception:
            pass
        record = coerce_device_record(str(key), raw)
        if record:
            devices[str(key)] = record
    trim_device_table(devices)
    raw_notes = data.get("notes") if isinstance(data.get("notes"), dict) else {}
    notes = {str(key): str(value)[:DEVICE_NOTE_MAX_LEN] for key, value in raw_notes.items() if isinstance(value, str) and value}
    with DEVICE_LOCK:
        DEVICE_STATE["devices"] = devices
        DEVICE_STATE["notes"] = notes
        DEVICE_STATE["prev"] = {}
        DEVICE_STATE["dirty"] = False
    return True

def save_device_state(path=None):
    """临时文件 + os.replace 原子写，面板被杀在写一半时不会留下坏文件。"""
    path = path or DEVICES_FILE
    with DEVICE_LOCK:
        payload = {
            "version": 1,
            "saved_at": int(time.time()),
            "devices": DEVICE_STATE["devices"],
            "notes": DEVICE_STATE["notes"],
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        DEVICE_STATE["dirty"] = False
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f".devices.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        remove_quietly(tmp_path)
        raise

def refresh_device_neighbours():
    local_ips = detect_local_ips()
    neighbours = detect_neighbours()
    upstream = detect_upstream_router()
    macs = {ip: info["mac"] for ip, info in neighbours.items() if info["mac"]}
    with DEVICE_LOCK:
        DEVICE_STATE["local_ips"] = local_ips
        DEVICE_STATE["neighbours"] = neighbours
        DEVICE_STATE["upstream_router"] = upstream
        DEVICE_STATE["macs"] = macs
        for key, device in DEVICE_STATE["devices"].items():
            if key in macs:
                device["mac"] = macs[key]

# 概览页的网速曲线：采样线程顺手用 /connections 的累计上传/下载算出每秒速率，保留最近 10 分钟
TRAFFIC_SERIES_POINTS = 120
TRAFFIC_SERIES = []
TRAFFIC_PREV = {}
TRAFFIC_LOCK = threading.Lock()

def is_listener_inbound(connection):
    """连接是不是从 listeners（其他地点经隧道连进来）进来的。本机的 TUN / mixed / socks 等入站叫 DEFAULT-*。"""
    meta = connection.get("metadata") if isinstance(connection, dict) else None
    name = str((meta or {}).get("inboundName") or "")
    return bool(name) and not name.startswith("DEFAULT-")

# 今日 / 昨日流量（按本地点方向）。mihomo 自己的累计值重启就清零、也不分方向，所以由面板按采样累加，
# 每天按北京时间 0 点换日（四个地点都在国内；容器是 UTC，不能用本地时间），存到 traffic_daily.json
TRAFFIC_DAILY_FILE = f"{MIHOMO_DIR}/traffic_daily.json"
TRAFFIC_DAY_OFFSET = 8 * 3600
TRAFFIC_DAILY = {"date": "", "down": 0, "up": 0, "yesterday": None, "dirty": False}

def traffic_day(now):
    return time.strftime("%Y-%m-%d", time.gmtime(now + TRAFFIC_DAY_OFFSET))

def add_daily_traffic(now, d_down, d_up):
    """调用方持有 TRAFFIC_LOCK。跨天时把今天的数挪到昨天；隔了不止一天就没有昨天的数据。"""
    day = traffic_day(now)
    if TRAFFIC_DAILY["date"] != day:
        if TRAFFIC_DAILY["date"] and TRAFFIC_DAILY["date"] == traffic_day(now - 86400):
            TRAFFIC_DAILY["yesterday"] = {"date": TRAFFIC_DAILY["date"], "down": TRAFFIC_DAILY["down"], "up": TRAFFIC_DAILY["up"]}
        elif TRAFFIC_DAILY["date"]:
            TRAFFIC_DAILY["yesterday"] = None
        TRAFFIC_DAILY.update({"date": day, "down": 0, "up": 0})
    TRAFFIC_DAILY["down"] += max(0, int(d_down))
    TRAFFIC_DAILY["up"] += max(0, int(d_up))
    TRAFFIC_DAILY["dirty"] = True

def traffic_daily_summary(now=None):
    now = time.time() if now is None else now
    with TRAFFIC_LOCK:
        add_daily_traffic(now, 0, 0)  # 过了 0 点还没有新采样时也要换日
        today = {"date": TRAFFIC_DAILY["date"], "down": TRAFFIC_DAILY["down"], "up": TRAFFIC_DAILY["up"]}
        yesterday = dict(TRAFFIC_DAILY["yesterday"]) if TRAFFIC_DAILY["yesterday"] else None
    return {"today": today, "yesterday": yesterday}

def load_traffic_daily(path=None):
    try:
        with open(path or TRAFFIC_DAILY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data.get("date") or "")):
        return False
    yesterday = data.get("yesterday")
    if isinstance(yesterday, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(yesterday.get("date") or "")):
        yesterday = {"date": yesterday["date"], "down": first_number(yesterday.get("down")), "up": first_number(yesterday.get("up"))}
    else:
        yesterday = None
    with TRAFFIC_LOCK:
        TRAFFIC_DAILY.update({"date": data["date"], "down": first_number(data.get("down")), "up": first_number(data.get("up")),
                              "yesterday": yesterday, "dirty": False})
    device_base = data.get("device_base")
    if isinstance(device_base, dict) and isinstance(device_base.get("base"), dict):
        base = {str(k)[:64]: first_number(v) for k, v in list(device_base["base"].items())[:4096]}
        with IKUAI_LOCK:
            IKUAI_TODAY.update({"date": str(device_base.get("date") or "")[:10], "base": base})
    return True

def save_traffic_daily(path=None):
    path = path or TRAFFIC_DAILY_FILE
    with TRAFFIC_LOCK:
        if not TRAFFIC_DAILY["date"]:
            return False
        payload = {k: TRAFFIC_DAILY[k] for k in ("date", "down", "up", "yesterday")}
        TRAFFIC_DAILY["dirty"] = False
    with IKUAI_LOCK:
        if IKUAI_TODAY["date"]:
            payload["device_base"] = {"date": IKUAI_TODAY["date"], "base": dict(IKUAI_TODAY["base"])}
    text = json.dumps(payload, separators=(",", ":"))
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f".traffic_daily.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        remove_quietly(tmp_path)
        raise
    return True

def is_listener_inbound(connection):
    """连接是不是从 listeners（其他地点经隧道连进来）进来的。本机的 TUN / mixed / socks 等入站叫 DEFAULT-*。"""
    meta = connection.get("metadata") if isinstance(connection, dict) else None
    name = str((meta or {}).get("inboundName") or "")
    return bool(name) and not name.startswith("DEFAULT-")

def record_traffic_sample(data, now=None):
    """mihomo 的上传/下载按“发起连接的一方”算。其他地点经 listeners 连进来的连接，
    对方发来的数据在 mihomo 里算上传，对本地点来说其实是下载，所以这部分要对调。"""
    now = time.time() if now is None else now
    data = data if isinstance(data, dict) else {}
    down = first_number(data.get("downloadTotal"))
    up = first_number(data.get("uploadTotal"))
    connections = data.get("connections") if isinstance(data.get("connections"), list) else []
    inbound = {}
    for conn in connections:
        if is_listener_inbound(conn) and conn.get("id"):
            inbound[conn["id"]] = (first_number(conn.get("upload")), first_number(conn.get("download")))
    with TRAFFIC_LOCK:
        prev_at = TRAFFIC_PREV.get("at")
        elapsed = now - prev_at if prev_at else 0
        if prev_at:
            # 累计值变小说明 mihomo 重启过，计数器从 0 重新开始，连接也都是新的
            restarted = down < TRAFFIC_PREV["down"] or up < TRAFFIC_PREV["up"]
            base_down, base_up = (0, 0) if restarted else (TRAFFIC_PREV["down"], TRAFFIC_PREV["up"])
            prev_inbound = {} if restarted else (TRAFFIC_PREV.get("inbound") or {})
            in_up = in_down = 0
            for conn_id, (c_up, c_down) in inbound.items():
                p_up, p_down = prev_inbound.get(conn_id, (0, 0))
                in_up += max(0, c_up - p_up)
                in_down += max(0, c_down - p_down)
            site_down = max(0, down - base_down - in_down + in_up)
            site_up = max(0, up - base_up - in_up + in_down)
            add_daily_traffic(now, site_down, site_up)
            # 曲线只画正常间隔的点；重启或控制器断过的那一段速率算不准，不画
            if not restarted and 0 < elapsed <= DEVICE_SAMPLE_INTERVAL * 6:
                TRAFFIC_SERIES.append({"t": int(now), "down": int(site_down / elapsed), "up": int(site_up / elapsed)})
                del TRAFFIC_SERIES[:-TRAFFIC_SERIES_POINTS]
        TRAFFIC_PREV.update({"at": now, "down": down, "up": up, "inbound": inbound})

def traffic_series():
    with TRAFFIC_LOCK:
        return [dict(item) for item in TRAFFIC_SERIES]

def sample_devices_once():
    ok, data = mihomo_api_get("/connections", timeout=3)
    if ok:
        record_traffic_sample(data)
        connections = data.get("connections") if isinstance(data, dict) else None
        apply_connections_sample(connections if isinstance(connections, list) else [])
        return True
    with DEVICE_LOCK:
        DEVICE_STATE["controller_error"] = str(data.get("error") or "controller unreachable")
        DEVICE_STATE["controller_error_at"] = int(time.time())
        # 控制器不可达时上一轮的速率和活动连接数已经过期，清零；累计值和 prev 保留，恢复后继续算增量
        for device in DEVICE_STATE["devices"].values():
            device["active_connections"] = 0
            device["rate_up"] = 0
            device["rate_down"] = 0
    return False

def device_sampler_loop():
    last_persist = time.time()
    last_neigh = 0
    while not DEVICE_SAMPLER_STOP.is_set():
        try:
            now = time.time()
            if now - last_neigh >= DEVICE_NEIGH_INTERVAL:
                refresh_device_neighbours()
                last_neigh = now
            sample_devices_once()
            try:
                poll_ikuai()
            except Exception as e:
                log.warning("拉取爱快终端失败：%s", e)
            if now - last_persist >= DEVICE_PERSIST_INTERVAL:
                with DEVICE_LOCK:
                    dirty = DEVICE_STATE["dirty"]
                if dirty:
                    save_device_state()
                with TRAFFIC_LOCK:
                    traffic_dirty = TRAFFIC_DAILY["dirty"]
                if traffic_dirty:
                    save_traffic_daily()
                last_persist = now
        except Exception as e:
            log.warning("设备采样失败：%s", e)
        DEVICE_SAMPLER_STOP.wait(DEVICE_SAMPLE_INTERVAL)

def stop_device_sampler():
    DEVICE_SAMPLER_STOP.set()
    try:
        save_device_state()
    except Exception as e:
        log.warning("退出时保存设备统计失败：%s", e)
    try:
        save_traffic_daily()
    except Exception as e:
        log.warning("退出时保存今日流量失败：%s", e)

def start_device_sampler():
    """只在 app.py 作为 __main__ 运行时调用（mihomo-manager.service 就是这么启动的）；
    测试 exec 这个模块时不会起线程。重复调用无效。"""
    global DEVICE_SAMPLER_STARTED
    if DEVICE_SAMPLER_STARTED:
        return False
    DEVICE_SAMPLER_STARTED = True
    load_device_state()
    load_traffic_daily()
    thread = threading.Thread(target=device_sampler_loop, name="device-sampler", daemon=True)
    thread.start()
    atexit.register(stop_device_sampler)
    return True

def device_kind(device):
    return device.get("kind") or ("gateway" if device.get("is_gateway") else "lan")

def device_payload(key, device, notes, now, neighbours=None, upstream=""):
    domains = device.get("domains") or {}
    top_domains = sorted(domains.items(), key=lambda item: item[1].get("bytes", 0), reverse=True)[:12]
    chains = sorted((device.get("chains") or {}).items(), key=lambda item: item[1], reverse=True)[:5]
    inbounds = {name: first_number(count) for name, count in (device.get("inbounds") or {}).items()}
    remote_ips = sorted((device.get("remote_ips") or {}).items(), key=lambda item: item[1], reverse=True)
    last_seen = first_number(device.get("last_seen"))
    kind = device_kind(device)
    neighbour = (neighbours or {}).get(key) or {}
    return {
        "ip": key,
        "kind": kind,
        "mac": device.get("mac") or neighbour.get("mac", ""),
        "note": notes.get(key, ""),
        "online": now - last_seen <= DEVICE_ONLINE_SECONDS,
        "is_gateway": kind == "gateway",
        "is_upstream_router": bool(upstream) and key == upstream,
        "seen_via": "traffic",
        "neighbour_state": neighbour.get("state", ""),
        "inbounds": inbounds,
        "inbound_types": [name for name, _ in sorted(inbounds.items(), key=lambda item: item[1], reverse=True)],
        "remote_ips": [ip for ip, _ in remote_ips[:DEVICE_REMOTE_IPS_SHOW]],
        "remote_ip_count": len(remote_ips),
        "last_seen": last_seen,
        "first_seen": first_number(device.get("first_seen")),
        "active_connections": first_number(device.get("active_connections")),
        "rate_up": first_number(device.get("rate_up")),
        "rate_down": first_number(device.get("rate_down")),
        "upload_total": first_number(device.get("upload_total")),
        "download_total": first_number(device.get("download_total")),
        "top_domains": [{"host": host, "bytes": entry.get("bytes", 0), "count": entry.get("count", 0)} for host, entry in top_domains],
        "chains": [{"label": label, "bytes": total} for label, total in chains],
        "last_host": device.get("last_host", ""),
    }

def neighbour_payload(ip, info, notes, upstream=""):
    """只在 ARP 表里、没有流量经过网关的同网段设备：灰色列出，计数全 0，不写入 devices.json。"""
    return {
        "ip": ip,
        "kind": "lan",
        "mac": info.get("mac", ""),
        "note": notes.get(ip, ""),
        "online": False,
        "is_gateway": False,
        "is_upstream_router": bool(upstream) and ip == upstream,
        "seen_via": "neighbour",
        "neighbour_state": info.get("state", ""),
        "inbounds": {},
        "inbound_types": [],
        "remote_ips": [],
        "remote_ip_count": 0,
        "last_seen": 0,
        "first_seen": 0,
        "active_connections": 0,
        "rate_up": 0,
        "rate_down": 0,
        "upload_total": 0,
        "download_total": 0,
        "top_domains": [],
        "chains": [],
        "last_host": "",
    }

def neighbour_only_ips(devices, neighbours, local_ips):
    """ARP 表里可见、但还没有流量记录的内网 IP：跳过 FAILED/INCOMPLETE、网关自己的地址和链路本地地址。
    只覆盖网关直连的网段，别的网段的设备 ARP 看不到。"""
    out = []
    for ip, info in neighbours.items():
        if ip in devices or info.get("state") not in NEIGHBOUR_VISIBLE_STATES:
            continue
        if source_kind(ip, local_ips) != "lan":
            continue
        try:
            if ipaddress.ip_address(ip).is_link_local:
                continue
        except ValueError:
            continue
        out.append(ip)
    return sorted(out, key=ip_sort_key)

def pseudo_device_active(item):
    """“本机/网关”“远程客户端”这两行只在当前有连接或有速率时显示，空着只是噪音。"""
    if item.get("kind") not in ("gateway", "remote"):
        return True
    return bool(item.get("active_connections") or item.get("rate_up") or item.get("rate_down"))

def devices_snapshot(now=None):
    """/api/devices 的响应。时间全是 epoch 秒，容器跑在 UTC，由浏览器按本地时区格式化。
    配置了爱快且至少成功拉取过一次时以爱快的终端为准，否则是纯 mihomo 连接视图。"""
    now = int(now if now is not None else time.time())
    ikuai = ikuai_settings()
    if ikuai_configured(ikuai):
        snapshot = ikuai_devices_snapshot(now, ikuai)
        if snapshot is not None:
            return snapshot
    controller = mihomo_controller_settings()
    with DEVICE_LOCK:
        notes = DEVICE_STATE["notes"]
        neighbours = DEVICE_STATE["neighbours"]
        upstream = DEVICE_STATE["upstream_router"]
        local_ips = DEVICE_STATE["local_ips"]
        devices = DEVICE_STATE["devices"]
        traffic = [device_payload(key, device, notes, now, neighbours, upstream) for key, device in devices.items()]
        traffic = [item for item in traffic if pseudo_device_active(item)]
        neighbour_rows = [neighbour_payload(ip, neighbours[ip], notes, upstream) for ip in neighbour_only_ips(devices, neighbours, local_ips)]
        sampled_at = DEVICE_STATE["sampled_at"]
        error = DEVICE_STATE["controller_error"]
        error_at = DEVICE_STATE["controller_error_at"]
    # 默认排序：在线优先，再按实时速率降序，再按最近活动；只在 ARP 里的设备永远排最后
    traffic.sort(key=lambda item: (not item["online"], -(item["rate_up"] + item["rate_down"]), -item["last_seen"]))
    items = traffic + neighbour_rows
    lan_rows = [item for item in items if item["kind"] == "lan"]
    return {
        "devices": items,
        "data_source": "mihomo",
        "ikuai": ikuai_status(ikuai),
        "proxy_via_router": None,
        "controller": {
            "reachable": bool(sampled_at) and not error,
            "error": error,
            "error_at": error_at,
            "base_url": controller["base_url"],
        },
        "sampled_at": sampled_at,
        "sample_interval": DEVICE_SAMPLE_INTERVAL,
        "online_seconds": DEVICE_ONLINE_SECONDS,
        "sampler_running": DEVICE_SAMPLER_STARTED,
        "upstream_router": upstream,
        "totals": {
            # devices / online 只算有流量的设备（兼容旧前端）；lan_* 把 ARP 可见的也算进去
            "devices": len(traffic),
            "online": sum(1 for item in traffic if item["online"]),
            "lan_total": len(lan_rows),
            "lan_online": sum(1 for item in lan_rows if item["online"]),
            "neighbour_only": len(neighbour_rows),
            "upload": sum(item["upload_total"] for item in traffic),
            "download": sum(item["download_total"] for item in traffic),
            "rate_up": sum(item["rate_up"] for item in traffic),
            "rate_down": sum(item["rate_down"] for item in traffic),
        },
    }

def devices_summary(now=None):
    """概览页的"设备 在线 N / 共 M"：lan_* 口径和设备页一致（含只在 ARP 里的设备；爱快模式下是爱快的终端数）。"""
    now = int(now if now is not None else time.time())
    ikuai = ikuai_settings()
    if ikuai_configured(ikuai):
        snapshot = ikuai_devices_snapshot(now, ikuai)
        if snapshot is not None:
            totals = snapshot["totals"]
            return {"online": totals["online"], "total": totals["devices"], "lan_online": totals["lan_online"], "lan_total": totals["lan_total"]}
    with DEVICE_LOCK:
        devices = DEVICE_STATE["devices"]
        records = list(devices.values())
        neighbour_count = len(neighbour_only_ips(devices, DEVICE_STATE["neighbours"], DEVICE_STATE["local_ips"]))
    def is_online(device):
        return now - first_number(device.get("last_seen")) <= DEVICE_ONLINE_SECONDS
    lan = [device for device in records if device_kind(device) == "lan"]
    return {
        "online": sum(1 for device in records if is_online(device)),
        "total": len(records),
        "lan_online": sum(1 for device in lan if is_online(device)),
        "lan_total": len(lan) + neighbour_count,
    }

def reset_device_totals():
    """累计流量、域名、链路全部清零；设备本身、首次/最近时间、MAC 和备注保留。"""
    with DEVICE_LOCK:
        for device in DEVICE_STATE["devices"].values():
            device["upload_total"] = 0
            device["download_total"] = 0
            device["domains"] = {}
            device["chains"] = {}
            device["last_host"] = ""
            device["last_start"] = ""
            device["rate_up"] = 0
            device["rate_down"] = 0
        DEVICE_STATE["dirty"] = True
    save_device_state()

def set_device_note(ip, note):
    ip = normalize_source_ip(ip)
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False, "IP 地址不合法"
    if not isinstance(note, str) or "\n" in note or "\r" in note or not is_safe_text(note, DEVICE_NOTE_MAX_LEN * 4) or len(note) > DEVICE_NOTE_MAX_LEN:
        return False, f"备注最多 {DEVICE_NOTE_MAX_LEN} 个字符，且不能包含换行"
    note = note.strip()
    with DEVICE_LOCK:
        if note:
            DEVICE_STATE["notes"][ip] = note
        else:
            DEVICE_STATE["notes"].pop(ip, None)
        DEVICE_STATE["dirty"] = True
    save_device_state()
    return True, "备注已保存" if note else "备注已清除"

# ---------------------------------------------------------------------------
# 爱快数据源
#
# 爱快是全家的网关：它有到 fake-ip 段（198.18.0.0/16）和 Telegram 段的静态路由指向本机，并对这些流量做了
# SNAT，所以 mihomo 看到的大部分代理连接来源都是爱快自己的 IP。爱快却认识每一台终端（含不认 DHCP 121
# 的 Android），有名字、MAC、实时速率和累计流量，所以配置了爱快之后设备列表以它为准：
#   - 设备行来自爱快的在线 / 离线终端；
#   - 直接以自己 IP 连到 mihomo 的设备，附上 mihomo 的代理明细（域名 / 出口）；
#   - 经爱快转发、来源是爱快 IP 的代理流量，单独作为“全家合计”展示，不当成一台设备。
# 爱快的数据只放内存，不落盘。
# ---------------------------------------------------------------------------
IKUAI_POLL_INTERVAL = 10          # 在线终端
IKUAI_SLOW_INTERVAL = 300         # DHCP 静态分配 + 离线终端
IKUAI_TIMEOUT = 5
IKUAI_PAGE_LIMIT = 100
IKUAI_MAX_PAGES = 10
IKUAI_APPS_CACHE_SECONDS = 30
IKUAI_APPS_TOP = 8
IKUAI_TOKEN_MAX_LEN = 1024
IKUAI_OK_CODES = (0, 20000)
# mihomo 的 fake-ip 地址也会出现在爱快的终端表里（MAC 是本机的），不是真设备
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
IKUAI_ONLINE_PATH = "/api/v4.0/monitoring/clients-online"
IKUAI_OFFLINE_PATH = "/api/v4.0/monitoring/clients-offline"
IKUAI_STATIC_PATH = "/api/v4.0/network/dhcp/static"
IKUAI_APPS_PATH = "/api/v4.0/monitoring/clients/app-protocols/load"

IKUAI_LOCK = threading.Lock()
IKUAI_STATE = {
    "online": [],             # clients-online 原始记录
    "offline": [],            # clients-offline 原始记录
    "static": [],             # DHCP 静态分配
    "fetched_at": 0,          # 上次在线终端拉取成功的时间
    "slow_fetched_at": 0,     # 上次 DHCP / 离线终端拉取成功的时间
    "polled_at": 0,           # 上次尝试拉取在线终端的时间（成功失败都算，控制轮询间隔）
    "slow_polled_at": 0,
    "error": "",              # 在线终端最近一次失败原因，成功后清空
    "slow_error": "",         # DHCP / 离线终端最近一次失败原因
    "error_at": 0,
    "apps_cache": {},         # ip -> (时间, 应用列表)
}

def ikuai_settings():
    env = read_env()
    return {"url": str(env.get("IKUAI_URL") or "").strip(), "token": str(env.get("IKUAI_TOKEN") or "").strip()}

def ikuai_configured(settings=None):
    settings = settings or ikuai_settings()
    return bool(settings["url"] and settings["token"])

def validate_ikuai_url(url):
    """只允许 http/https、内网或回环 IP、或不带空格的主机名；返回 (ok, 规范化后的 URL 或错误信息)。
    面板会带着 Token 去请求这个地址，不能让它指向公网 IP。"""
    url = str(url or "").strip()
    if not url:
        return False, "请填写爱快地址"
    if len(url) > 300 or any(ch.isspace() for ch in url):
        return False, "爱快地址不能包含空格"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False, "爱快地址格式不正确"
    if parts.scheme not in ("http", "https"):
        return False, "爱快地址必须以 http:// 或 https:// 开头"
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return False, "爱快地址只填协议、主机和端口，例如 https://10.10.10.253"
    host = parts.hostname or ""
    if not host:
        return False, "爱快地址缺少主机"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if not (addr.is_private or addr.is_loopback or addr.is_link_local):
            return False, "爱快地址必须是内网 IP"
    elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        return False, "爱快主机名不合法"
    netloc = f"[{host}]" if addr is not None and addr.version == 6 else host
    if port:
        netloc += f":{port}"
    return True, f"{parts.scheme}://{netloc}"

def validate_ikuai_token(token):
    if not is_safe_text(token, IKUAI_TOKEN_MAX_LEN) or any(ch.isspace() for ch in token):
        return False
    return all(32 < ord(ch) < 127 for ch in token)

def ikuai_unwrap(envelope):
    """爱快的响应：{code: 0|20000 表示成功, message, data, results}；有 data（且不是 null）就取 data，否则取 results。"""
    if not isinstance(envelope, dict):
        return False, "爱快返回的不是 JSON 对象"
    try:
        code = int(envelope.get("code"))
    except (TypeError, ValueError):
        code = None
    if code not in IKUAI_OK_CODES:
        message = str(envelope.get("message") or "").strip() or f"错误码 {envelope.get('code')}"
        return False, f"爱快返回错误：{message}"
    payload = envelope.get("data")
    if payload is None:
        payload = envelope.get("results")
    return True, payload

def ikuai_http_get(base_url, token, path, params=None, timeout=IKUAI_TIMEOUT):
    """GET 爱快接口，返回解析后的 JSON。爱快用自签证书，只对这个用户配置的地址关闭证书校验。
    HTTP >= 400 时抛 RuntimeError（带上爱快的 message）。测试里会替换掉这个函数。"""
    url = base_url.rstrip("/") + path + ("?" + urlencode(params) if params else "")
    req = urlrequest.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "mihomo-web-manager",
    })
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urlrequest.urlopen(req, timeout=timeout, context=context) as resp:
            body = resp.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except urlerror.HTTPError as e:
        detail = ""
        try:
            data = json.loads(e.read(64 * 1024).decode("utf-8", "replace"))
            detail = str(data.get("message") or "") if isinstance(data, dict) else ""
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}" + (f"：{detail}" if detail else ""))
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("爱快返回的不是 JSON")

def ikuai_call(base_url, token, path, params=None):
    try:
        envelope = ikuai_http_get(base_url, token, path, params)
    except Exception as e:
        reason = getattr(e, "reason", None)
        return False, f"请求爱快失败：{reason or e}"
    return ikuai_unwrap(envelope)

def ikuai_fetch_pages(base_url, token, path, list_key, total_key):
    """分页拉完一个列表（每页 IKUAI_PAGE_LIMIT 条，最多 IKUAI_MAX_PAGES 页）。返回 (ok, 列表或错误信息)。"""
    items = []
    for page in range(1, IKUAI_MAX_PAGES + 1):
        ok, payload = ikuai_call(base_url, token, path, {"limit": IKUAI_PAGE_LIMIT, "page": page})
        if not ok:
            return False, payload
        if not isinstance(payload, dict):
            return False, "爱快返回的数据格式不对"
        batch = payload.get(list_key)
        batch = [item for item in batch if isinstance(item, dict)] if isinstance(batch, list) else []
        items.extend(batch)
        total = first_number(payload.get(total_key))
        if not batch or len(batch) < IKUAI_PAGE_LIMIT or len(items) >= total:
            break
    return True, items

def ikuai_record_error(key, message, now):
    with IKUAI_LOCK:
        IKUAI_STATE[key] = str(message)[:300]
        IKUAI_STATE["error_at"] = now

# 爱快只给离线终端填 today_total，在线终端一直是 0。面板自己记每台终端当天第一次看到时的累计流量，
# 今日流量 = 现在的累计 - 这个基准（北京时间 0 点换日，和概览的今日流量一致），基准随 traffic_daily.json 保存
IKUAI_TODAY = {"date": "", "base": {}}

def ikuai_client_key(client):
    return str(client.get("mac") or "").strip().lower() or str(client.get("ip_addr") or "").strip()

def ikuai_client_total(client):
    return max(0, first_number(client.get("total_up"))) + max(0, first_number(client.get("total_down")))

def update_ikuai_today(clients, now):
    """调用方持有 IKUAI_LOCK。换日时把基准换成当前累计；累计变小（路由器重启清零）时基准归零。"""
    day = traffic_day(now)
    if IKUAI_TODAY["date"] != day:
        IKUAI_TODAY["date"] = day
        IKUAI_TODAY["base"] = {}
    base = IKUAI_TODAY["base"]
    for client in clients:
        key = ikuai_client_key(client)
        if not key:
            continue
        total = ikuai_client_total(client)
        if key not in base:
            base[key] = total
        elif total < base[key]:
            base[key] = 0
    with TRAFFIC_LOCK:
        TRAFFIC_DAILY["dirty"] = True

def ikuai_today_total(client, now):
    """调用方持有 IKUAI_LOCK。"""
    reported = max(0, first_number(client.get("today_total")))
    if IKUAI_TODAY["date"] != traffic_day(now):
        return reported
    base = IKUAI_TODAY["base"].get(ikuai_client_key(client))
    counted = ikuai_client_total(client) - base if base is not None else 0
    return max(reported, counted, 0)

def poll_ikuai(now=None, force_slow=False, settings=None):
    """采样线程每轮调一次：在线终端每 IKUAI_POLL_INTERVAL 秒、DHCP 和离线终端每 IKUAI_SLOW_INTERVAL 秒。
    失败时保留上次的数据，只记录错误。没配置爱快就什么都不做。"""
    now = int(now if now is not None else time.time())
    settings = settings or ikuai_settings()
    if not ikuai_configured(settings):
        return False
    url, token = settings["url"], settings["token"]
    with IKUAI_LOCK:
        due_fast = now - IKUAI_STATE["polled_at"] >= IKUAI_POLL_INTERVAL
        due_slow = force_slow or not IKUAI_STATE["slow_polled_at"] or now - IKUAI_STATE["slow_polled_at"] >= IKUAI_SLOW_INTERVAL
        if due_fast:
            IKUAI_STATE["polled_at"] = now
        if due_slow:
            IKUAI_STATE["slow_polled_at"] = now
    if due_fast:
        ok, result = ikuai_fetch_pages(url, token, IKUAI_ONLINE_PATH, "data", "total")
        if ok:
            with IKUAI_LOCK:
                IKUAI_STATE["online"] = result
                IKUAI_STATE["fetched_at"] = now
                IKUAI_STATE["error"] = ""
                update_ikuai_today(result, now)
        else:
            ikuai_record_error("error", result, now)
    if due_slow:
        ok_static, statics = ikuai_fetch_pages(url, token, IKUAI_STATIC_PATH, "static_data", "static_total")
        ok_offline, offline = ikuai_fetch_pages(url, token, IKUAI_OFFLINE_PATH, "offline_data", "offline_total")
        with IKUAI_LOCK:
            if ok_static:
                IKUAI_STATE["static"] = statics
            if ok_offline:
                IKUAI_STATE["offline"] = offline
            if ok_static and ok_offline:
                IKUAI_STATE["slow_fetched_at"] = now
                IKUAI_STATE["slow_error"] = ""
        if not (ok_static and ok_offline):
            ikuai_record_error("slow_error", statics if not ok_static else offline, now)
    return True

def reset_ikuai_state():
    """改了爱快地址或 Token 后，旧路由器的数据不能再显示。"""
    with IKUAI_LOCK:
        IKUAI_STATE.update({
            "online": [], "offline": [], "static": [], "fetched_at": 0, "slow_fetched_at": 0,
            "polled_at": 0, "slow_polled_at": 0, "error": "", "slow_error": "", "error_at": 0, "apps_cache": {},
        })

def ikuai_status(settings=None):
    settings = settings or ikuai_settings()
    with IKUAI_LOCK:
        fetched_at = IKUAI_STATE["fetched_at"]
        error = IKUAI_STATE["error"] or IKUAI_STATE["slow_error"]
        error_at = IKUAI_STATE["error_at"]
        fast_error = IKUAI_STATE["error"]
    configured = ikuai_configured(settings)
    return {
        "configured": configured,
        "ok": configured and bool(fetched_at) and not fast_error,
        "error": error if configured else "",
        "error_at": error_at if configured else 0,
        "fetched_at": fetched_at if configured else 0,
    }

def ikuai_settings_payload():
    settings = ikuai_settings()
    status = ikuai_status(settings)
    return {
        "url": settings["url"],
        "token_set": bool(settings["token"]),
        "configured": status["configured"],
        "last_ok_at": status["fetched_at"],
        "last_error": status["error"],
        "last_error_at": status["error_at"],
    }

def save_ikuai_settings(data):
    """保存 IKUAI_URL / IKUAI_TOKEN。URL 留空表示不再使用爱快；Token 留空保持原值，clear_token 才清除。"""
    data = data if isinstance(data, dict) else {}
    current = ikuai_settings()
    url = str(data.get("url") or "").strip()
    if url:
        ok, result = validate_ikuai_url(url)
        if not ok:
            return False, result
        url = result
    token_raw = data.get("token")
    token = str(token_raw).strip() if isinstance(token_raw, str) else ""
    clear_token = data.get("clear_token") is True or is_true(data.get("clear_token"))
    if clear_token:
        token = ""
    elif not token:
        token = current["token"]
    elif not validate_ikuai_token(token):
        return False, "Token 不合法（不能包含空格或特殊字符）"
    write_env({"IKUAI_URL": url, "IKUAI_TOKEN": token})
    if url != current["url"] or token != current["token"]:
        reset_ikuai_state()
    if not url:
        return True, "已停用爱快数据源"
    if clear_token:
        return True, "Token 已清除，设备列表改回使用 mihomo 连接数据"
    return True, "爱快设置已保存" + ("" if token else "，但还没有填写 Token")

def test_ikuai_connection(data):
    """用提交的值（空字段回落到已保存的值）拉一页在线终端，不写 .env。"""
    data = data if isinstance(data, dict) else {}
    saved = ikuai_settings()
    url = str(data.get("url") or "").strip() or saved["url"]
    token_raw = data.get("token")
    token = (str(token_raw).strip() if isinstance(token_raw, str) else "") or saved["token"]
    ok, result = validate_ikuai_url(url)
    if not ok:
        return False, result
    if not token:
        return False, "请填写 API Token"
    if not validate_ikuai_token(token):
        return False, "Token 不合法（不能包含空格或特殊字符）"
    ok, payload = ikuai_call(result, token, IKUAI_ONLINE_PATH, {"limit": IKUAI_PAGE_LIMIT, "page": 1})
    if not ok:
        return False, payload
    if not isinstance(payload, dict):
        return False, "爱快返回的数据格式不对"
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    total = first_number(payload.get("total")) or len(rows)
    return True, f"连接正常，在线终端 {total} 台"

def is_fake_ip(ip):
    try:
        return ipaddress.ip_address(ip) in FAKE_IP_NETWORK
    except ValueError:
        return False

def ikuai_text(value):
    """爱快的 hostname 有时是 URL 编码的（空格成了 %20），解一层；"Unknown" 之类的占位当空。"""
    text = str(value or "").strip()
    if "%" in text:
        try:
            text = unquote(text).strip()
        except Exception:
            pass
    return "" if text.lower() in ("unknown", "--", "null", "none") else text[:120]

def ikuai_rate(client, number_key, text_key):
    """当前速率（字节/秒）。

    在线终端里 upload / download 是当前速率：实测样本里它们只有 0~3000 量级，而同一条记录的
    total_up / total_down 是几十 MB 到几百 GB 的累计字节；uprate / downrate 在这版固件里是空串
    （老固件里是格式化好的文字）。所以以 upload / download 为准，取不到数字时才看 uprate / downrate。"""
    value = client.get(number_key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    for raw in (value, client.get(text_key)):
        text = str(raw or "").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return int(float(text))
    return 0

def ikuai_static_index(statics):
    by_mac, by_ip = {}, {}
    for item in statics:
        mac = str(item.get("mac") or "").lower()
        ip = str(item.get("ip_addr") or "")
        if mac:
            by_mac.setdefault(mac, item)
        if ip:
            by_ip.setdefault(ip, item)
    return by_mac, by_ip

def ikuai_device_name(ip, client, static, notes):
    """名字优先级：面板备注 → 爱快备注 → DHCP 静态分配的标签（纯数字跳过）→ 终端名 → 主机名。返回 (名字, 来源)。"""
    note = notes.get(ip, "")
    if note:
        return note, "note"
    comment = ikuai_text(client.get("comment"))
    if comment:
        return comment, "comment"
    tag = ikuai_text(static.get("tagname"))
    if tag and not tag.isdigit():
        return tag, "dhcp_tag"
    term = ikuai_text(client.get("termname")) or ikuai_text(static.get("termname"))
    if term:
        return term, "termname"
    host = ikuai_text(client.get("hostname")) or ikuai_text(static.get("hostname"))
    if host:
        return host, "hostname"
    return "", ""

def proxy_detail(device, notes, now):
    """mihomo 记录里前端需要的那部分：累计、实时、域名、出口。"""
    payload = device_payload(device.get("ip", ""), device, notes, now)
    return {
        "upload": payload["upload_total"],
        "download": payload["download_total"],
        "rate_up": payload["rate_up"],
        "rate_down": payload["rate_down"],
        "active_connections": payload["active_connections"],
        "top_domains": payload["top_domains"],
        "chains": payload["chains"],
    }

def ikuai_device_rows(online, offline, statics, notes, mihomo_devices, local_ips, router_ips, now, today_totals=None):
    """爱快的在线 + 离线终端合成设备行：先按 MAC 再按 IP 去重（在线优先），跳过 fake-ip 段。
    today_totals：id(client) -> 今日流量（由调用方在 IKUAI_LOCK 里算好）。"""
    today_totals = today_totals or {}
    static_by_mac, static_by_ip = ikuai_static_index(statics)
    rows = []
    seen_macs, seen_ips = set(), set()
    for is_online, clients in ((True, online), (False, offline)):
        for client in clients:
            ip = str(client.get("ip_addr") or "").strip()
            mac = str(client.get("mac") or "").strip().lower()
            if not ip or is_fake_ip(ip):
                continue
            try:
                if ipaddress.ip_address(ip).is_loopback:
                    continue
            except ValueError:
                continue
            if (mac and mac in seen_macs) or ip in seen_ips:
                continue
            if mac:
                seen_macs.add(mac)
            seen_ips.add(ip)
            static = static_by_mac.get(mac) or static_by_ip.get(ip) or {}
            name, name_source = ikuai_device_name(ip, client, static, notes)
            role = "proxy_gateway" if ip in local_ips else ("router" if ip in router_ips else "")
            ssid = ikuai_text(client.get("ssid"))
            signal_value = first_number(client.get("signal"))
            rate_up = ikuai_rate(client, "upload", "uprate") if is_online else 0
            rate_down = ikuai_rate(client, "download", "downrate") if is_online else 0
            total_up = max(0, first_number(client.get("total_up")))
            total_down = max(0, first_number(client.get("total_down")))
            connections = max(0, first_number(client.get("connect_num"))) if is_online else 0
            mihomo = mihomo_devices.get(ip)
            row = {
                "ip": ip,
                "mac": mac,
                "name": name,
                "name_source": name_source,
                "note": notes.get(ip, ""),
                "vendor": ikuai_text(client.get("client_vendor")),
                "model": ikuai_text(client.get("client_model")),
                "type": ikuai_text(client.get("device_type")) or ikuai_text(client.get("client_type")),
                "online": is_online,
                "rate_up": rate_up,
                "rate_down": rate_down,
                "total_up": total_up,
                "total_down": total_down,
                "today_total": today_totals.get(id(client), 0),
                "connections": connections,
                "since": str(client.get("uptime") or "")[:32] if is_online else "",
                "offline_at": 0 if is_online else first_number(client.get("logout_time")),
                "interface": str(client.get("interface") or "")[:32],
                "source": "ikuai",
                "role": role,
                "kind": "lan",
                "seen_via": "ikuai",
                "is_gateway": False,
                "is_upstream_router": False,
                # 和 mihomo 行同名的字段，前端排序沿用
                "upload_total": total_up,
                "download_total": total_down,
                "active_connections": connections,
                # 爱快的 timestamp 是上线时间，不是最近活动；在线就算“现在”
                "last_seen": int(now) if is_online else first_number(client.get("logout_time")),
            }
            if ssid or signal_value:
                row["wireless"] = {"ssid": ssid, "signal": signal_value}
            # 爱快自己 IP 下的代理流量是全家合计（proxy_via_router），不挂在路由器这一行上
            if mihomo is not None and device_kind(mihomo) == "lan" and ip not in router_ips:
                row["proxy"] = proxy_detail(mihomo, notes, now)
            rows.append(row)
    return rows

def ikuai_router_ips(upstream, settings):
    ips = {upstream} if upstream else set()
    try:
        host = urlsplit(settings["url"]).hostname or ""
        ipaddress.ip_address(host)
        ips.add(host)
    except ValueError:
        pass
    return ips

def ikuai_devices_snapshot(now, settings):
    """爱快模式下的 /api/devices；爱快没配置或从没成功过时返回 None，调用方退回纯 mihomo 视图。"""
    with IKUAI_LOCK:
        if not IKUAI_STATE["fetched_at"]:
            return None
        online = list(IKUAI_STATE["online"])
        offline = list(IKUAI_STATE["offline"])
        statics = list(IKUAI_STATE["static"])
        today_totals = {id(client): ikuai_today_total(client, now) for client in online + offline}
    controller = mihomo_controller_settings()
    with DEVICE_LOCK:
        notes = dict(DEVICE_STATE["notes"])
        upstream = DEVICE_STATE["upstream_router"]
        local_ips = set(DEVICE_STATE["local_ips"])
        mihomo_devices = DEVICE_STATE["devices"]
        router_ips = ikuai_router_ips(upstream, settings)
        rows = ikuai_device_rows(online, offline, statics, notes, mihomo_devices, local_ips, router_ips, now, today_totals)
        matched = {row["ip"] for row in rows}
        extra = []
        for key, device in mihomo_devices.items():
            kind = device_kind(device)
            if kind in ("remote", "gateway"):
                extra.append(device_payload(key, device, notes, now, None, upstream))
            elif key not in matched and key != upstream and now - first_number(device.get("last_seen")) <= DEVICE_ONLINE_SECONDS:
                # 爱快不认识、但正在直连 mihomo 的内网来源（比如别的网段），不丢
                extra.append(device_payload(key, device, notes, now, None, upstream))
        for item in extra:
            item["source"] = "mihomo"
        via_router = None
        if upstream and upstream in mihomo_devices:
            detail = proxy_detail(mihomo_devices[upstream], notes, now)
            via_router = {
                "ip": upstream,
                "upload_total": detail["upload"],
                "download_total": detail["download"],
                "rate_up": detail["rate_up"],
                "rate_down": detail["rate_down"],
                "active_connections": detail["active_connections"],
                "top_domains": detail["top_domains"],
                "chains": detail["chains"],
            }
        sampled_at = DEVICE_STATE["sampled_at"]
        error = DEVICE_STATE["controller_error"]
        error_at = DEVICE_STATE["controller_error_at"]
    # 默认排序：在线优先，再按当前总速率降序，再按今日流量降序
    rows.sort(key=lambda item: (not item["online"], -(item["rate_up"] + item["rate_down"]), -item["today_total"]))
    extra = [item for item in extra if pseudo_device_active(item)]
    extra.sort(key=lambda item: (not item["online"], -(item["rate_up"] + item["rate_down"])))
    online_rows = [row for row in rows if row["online"]]
    return {
        "devices": rows + extra,
        "data_source": "ikuai",
        "ikuai": ikuai_status(settings),
        "ikuai_url": settings["url"],
        "proxy_via_router": via_router,
        "controller": {
            "reachable": bool(sampled_at) and not error,
            "error": error,
            "error_at": error_at,
            "base_url": controller["base_url"],
        },
        "sampled_at": sampled_at,
        "sample_interval": DEVICE_SAMPLE_INTERVAL,
        "ikuai_interval": IKUAI_POLL_INTERVAL,
        "online_seconds": DEVICE_ONLINE_SECONDS,
        "sampler_running": DEVICE_SAMPLER_STARTED,
        "upstream_router": upstream,
        "totals": {
            "devices": len(rows),
            "online": len(online_rows),
            "lan_total": len(rows),
            "lan_online": len(online_rows),
            "neighbour_only": 0,
            "upload": sum(row["total_up"] for row in rows),
            "download": sum(row["total_down"] for row in rows),
            "today": sum(row["today_total"] for row in rows),
            "rate_up": sum(row["rate_up"] for row in rows),
            "rate_down": sum(row["rate_down"] for row in rows),
        },
    }

def ikuai_lookup_mac(ip):
    with IKUAI_LOCK:
        for client in list(IKUAI_STATE["online"]) + list(IKUAI_STATE["offline"]):
            if str(client.get("ip_addr") or "").strip() == ip and client.get("mac"):
                return str(client.get("mac")).strip().lower()
    return ""

def device_apps(ip, now=None):
    """/api/devices/<ip>/apps：爱快按应用协议统计的流量，前 IKUAI_APPS_TOP 个（按累计）。返回 (响应, HTTP 状态)。"""
    now = int(now if now is not None else time.time())
    ip = normalize_source_ip(ip)
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return {"success": False, "message": "IP 地址不合法"}, 400
    settings = ikuai_settings()
    if not ikuai_configured(settings):
        return {"success": False, "message": "还没有配置爱快数据源"}, 400
    with IKUAI_LOCK:
        cached = IKUAI_STATE["apps_cache"].get(ip)
    if cached and now - cached[0] < IKUAI_APPS_CACHE_SECONDS:
        return {"success": True, "ip": ip, "apps": cached[1], "cached": True}, 200
    mac = ikuai_lookup_mac(ip)
    if not mac:
        return {"success": False, "message": "爱快里没有这台设备"}, 404
    ok, payload = ikuai_call(settings["url"], settings["token"], IKUAI_APPS_PATH, {"ip": ip, "mac": mac, "limit": 20})
    if not ok:
        return {"success": False, "message": payload}, 502
    rows = payload.get("data") if isinstance(payload, dict) else payload
    rows = [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
    rows.sort(key=lambda item: first_number(item.get("total")), reverse=True)
    apps = [{
        "appname": str(item.get("appname") or "未知应用")[:80],
        "total": max(0, first_number(item.get("total"))),
        "total_up": max(0, first_number(item.get("total_up"))),
        "total_down": max(0, first_number(item.get("total_down"))),
        "rate_up": max(0, first_number(item.get("upload"))),
        "rate_down": max(0, first_number(item.get("download"))),
        "connections": max(0, first_number(item.get("conn_cnt"))),
    } for item in rows[:IKUAI_APPS_TOP]]
    with IKUAI_LOCK:
        cache = IKUAI_STATE["apps_cache"]
        for key in [key for key, value in cache.items() if now - value[0] >= IKUAI_APPS_CACHE_SECONDS]:
            cache.pop(key, None)
        cache[ip] = (now, apps)
    return {"success": True, "ip": ip, "apps": apps, "cached": False}, 200

def collect_overview():
    env = read_env()
    running = is_service_active("mihomo")
    controller = mihomo_controller_settings()
    connections_ok, connections = mihomo_api_get("/connections")
    version_ok, version = mihomo_api_get("/version")
    proxies_ok, proxies = mihomo_api_get("/proxies")
    connection_list = connections.get("connections") if isinstance(connections.get("connections"), list) else []
    proxy_groups = proxy_group_summary(proxies) if proxies_ok else []
    local_effective, local_auto, _ = effective_local_networks(env.get("LOCAL_CIDR", ""))
    return {
        "running": running,
        "controller": {
            "base_url": controller["base_url"],
            "reachable": bool(connections_ok or version_ok or proxies_ok),
            "error": "" if (connections_ok or version_ok or proxies_ok) else connections.get("error", "controller unreachable"),
        },
        "panel_version": PANEL_VERSION,
        "site_name": env.get("SITE_NAME", ""),
        "core_version": version.get("version", "") if version_ok else "",
        "connections_count": len(connection_list),
        "download_total": first_number(connections.get("downloadTotal")),
        "upload_total": first_number(connections.get("uploadTotal")),
        "memory": first_number(connections.get("memory")),
        "proxy_groups": proxy_groups,
        "traffic_series": traffic_series(),
        "traffic_daily": traffic_daily_summary(),
        "devices": devices_summary(),
        "log_levels": log_level_summary(),
        "settings": {
            "config_mode": env.get("CONFIG_MODE", "airport"),
            "subscription_count": subscription_count(env),
            "cron_sub_enabled": env.get("CRON_SUB_ENABLED") == "true",
            "cron_sub_sched": env.get("CRON_SUB_SCHED", ""),
            "cron_sub_schedule": cron_to_mode(env.get("CRON_SUB_SCHED", ""), ""),
            "cron_geo_enabled": env.get("CRON_GEO_ENABLED") == "true",
            "cron_geo_schedule": cron_to_mode(env.get("CRON_GEO_SCHED", ""), ""),
            "auto_update": auto_update_settings(env),
            "timezone": server_timezone_info(),
            "notify_api": env.get("NOTIFY_API") == "true",
            "local_cidr": env.get("LOCAL_CIDR", ""),
            "local_networks_effective": local_effective,
            "local_networks_auto": local_auto,
            "last_subscription": last_subscription_state(),
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

# ==========================================
# 自动更新（面板侧）：设置校验、定时任务、状态文件、锁、后台启动。
# 真正干活的是同目录的 auto_update.py（cron 调用，import 本文件的函数，不启动 Flask）。
# ==========================================
AUTO_UPDATE_JOB_ID = "# MIHOMO_AUTO_UPDATE"
AUTO_UPDATE_SCRIPT = f"{MANAGER_DIR}/auto_update.py"
AUTO_UPDATE_STATE_FILE = f"{MIHOMO_DIR}/auto_update_state.json"
AUTO_UPDATE_LOG = "/var/log/mihomo-auto-update.log"
AUTO_UPDATE_LOCK_NAME = "mihomo-auto-update.lock"
AUTO_UPDATE_ITEMS = ("ui", "core", "panel")
AUTO_UPDATE_ITEM_LABELS = {"ui": "Dashboard", "core": "mihomo 内核", "panel": "管理面板"}
AUTO_UPDATE_RESULT_LABELS = {
    "updated": "已更新", "up_to_date": "已是最新", "skipped": "已跳过", "failed": "失败",
    "rolled_back": "已回滚", "started": "升级已开始", "available": "有可用更新", "not_due": "未到间隔",
}
AUTO_UPDATE_DEFAULTS = {"enabled": True, "time": "04:00", "core_min_age_days": 3, "panel_min_age_days": 0, "ui_interval_days": 7}
AUTO_UPDATE_ENV_KEYS = {
    "enabled": "AUTO_UPDATE_ENABLED",
    "time": "AUTO_UPDATE_TIME",
    "core_min_age_days": "AUTO_UPDATE_CORE_MIN_AGE_DAYS",
    "panel_min_age_days": "AUTO_UPDATE_PANEL_MIN_AGE_DAYS",
    "ui_interval_days": "AUTO_UPDATE_UI_INTERVAL_DAYS",
}
AUTO_UPDATE_RANGES = {"core_min_age_days": (0, 90), "panel_min_age_days": (0, 90), "ui_interval_days": (1, 365)}
AUTO_UPDATE_NUMBER_LABELS = {"core_min_age_days": "内核发布满几天才更新", "panel_min_age_days": "面板发布满几天才更新", "ui_interval_days": "Dashboard 每隔几天更新"}
# 面板升级写下 started 后，新进程启动时把它标成 updated；超过这个时间还没到新版本就算失败
AUTO_UPDATE_PANEL_PENDING_SECONDS = 600
AUTO_UPDATE_DRY_RUN_TIMEOUT = 180

def auto_update_time_valid(value):
    match = re.fullmatch(r"(\d{2}):(\d{2})", str(value or ""))
    return bool(match) and int(match.group(1)) <= 23 and int(match.group(2)) <= 59

def auto_update_int(value):
    """只认非负整数（数字或纯数字字符串），其他一律 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    return int(text) if re.fullmatch(r"\d{1,6}", text) else None

def auto_update_settings(env=None):
    """从 .env 读设置；缺失或非法的值回落到默认值（面板保存时会做严格校验）。"""
    env = read_env() if env is None else env
    settings = dict(AUTO_UPDATE_DEFAULTS)
    enabled = str(env.get(AUTO_UPDATE_ENV_KEYS["enabled"], "") or "").strip().lower()
    if enabled in ("true", "false"):
        settings["enabled"] = enabled == "true"
    time_value = str(env.get(AUTO_UPDATE_ENV_KEYS["time"], "") or "").strip()
    if auto_update_time_valid(time_value):
        settings["time"] = time_value
    for key, (low, high) in AUTO_UPDATE_RANGES.items():
        number = auto_update_int(env.get(AUTO_UPDATE_ENV_KEYS[key]))
        if number is not None and low <= number <= high:
            settings[key] = number
    return settings

def validate_auto_update_settings(data):
    """面板提交的设置严格校验。返回 (ok, 错误说明, 规范化后的设置)。"""
    data = data if isinstance(data, dict) else {}
    errors = []
    enabled = data.get("enabled", AUTO_UPDATE_DEFAULTS["enabled"])
    if isinstance(enabled, str) and enabled.strip().lower() in ("true", "false"):
        enabled = enabled.strip().lower() == "true"
    if not isinstance(enabled, bool):
        errors.append("开关只能是 true 或 false")
    time_value = str(data.get("time", "") or "").strip()
    if not auto_update_time_valid(time_value):
        errors.append("更新时间格式应为 HH:MM（00:00–23:59）")
    normalized = {"enabled": enabled, "time": time_value}
    for key, (low, high) in AUTO_UPDATE_RANGES.items():
        number = auto_update_int(data.get(key, AUTO_UPDATE_DEFAULTS[key]))
        if number is None or not low <= number <= high:
            errors.append(f"{AUTO_UPDATE_NUMBER_LABELS[key]}应为 {low}–{high} 的整数")
        normalized[key] = number
    if errors:
        return False, "；".join(errors), None
    return True, "", normalized

def auto_update_env_updates(settings):
    return {
        AUTO_UPDATE_ENV_KEYS["enabled"]: "true" if settings["enabled"] else "false",
        AUTO_UPDATE_ENV_KEYS["time"]: settings["time"],
        **{AUTO_UPDATE_ENV_KEYS[key]: str(settings[key]) for key in AUTO_UPDATE_RANGES},
    }

def auto_update_cron_schedule(time_value):
    hour, minute = parse_daily_time(time_value, 4)
    return f"{minute} {hour} * * *"

def auto_update_cron_command():
    # 脚本自己把可读日志追加到 AUTO_UPDATE_LOG；这里只把异常栈（stderr）也收进去，stdout 丢掉避免重复
    return f"python3 {AUTO_UPDATE_SCRIPT} >/dev/null 2>>{AUTO_UPDATE_LOG}"

def auto_update_cron_line(settings):
    return f"{auto_update_cron_schedule(settings['time'])} {auto_update_cron_command()} {AUTO_UPDATE_JOB_ID}"

def apply_auto_update_cron(settings=None):
    settings = settings or auto_update_settings()
    return update_cron(AUTO_UPDATE_JOB_ID, auto_update_cron_schedule(settings["time"]), auto_update_cron_command(), bool(settings["enabled"]))

def ensure_auto_update_cron(settings=None):
    """面板启动、保存设置时调用：crontab 里的自动更新行和 .env 不一致才重写，关闭时删掉。"""
    settings = settings or auto_update_settings()
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        current = [line for line in (res.stdout or "").splitlines() if AUTO_UPDATE_JOB_ID in line]
    except Exception:
        current = None
    desired = [auto_update_cron_line(settings)] if settings["enabled"] else []
    if current == desired:
        return True, ""
    return apply_auto_update_cron(settings)

def read_auto_update_state(path=None):
    path = path or AUTO_UPDATE_STATE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    data["items"] = {key: (items.get(key) if isinstance(items.get(key), dict) else {}) for key in AUTO_UPDATE_ITEMS}
    return data

def write_auto_update_state(state, path=None):
    write_text_atomic(path or AUTO_UPDATE_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

def auto_update_lock_path():
    directory = "/run" if os.path.isdir("/run") and os.access("/run", os.W_OK) else tempfile.gettempdir()
    return os.path.join(directory, AUTO_UPDATE_LOCK_NAME)

def acquire_auto_update_lock(path=None):
    """单实例锁：拿到返回文件描述符（进程退出自动释放），已被占用返回 None。"""
    path = path or auto_update_lock_path()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd

def release_auto_update_lock(fd):
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

def auto_update_lock_busy(path=None):
    try:
        fd = acquire_auto_update_lock(path)
    except OSError:
        return False
    if fd is None:
        return True
    release_auto_update_lock(fd)
    return False

def run_notify(title, content):
    """后台发通知，不等结果（notify.sh 自己会写 /var/log/mihomo-notify.log）。
    title/content 应由 notify_event 生成；这里只负责投递。"""
    try:
        subprocess.Popen(["bash", f"{SCRIPT_DIR}/notify.sh", title, content],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        return True
    except Exception as e:
        log.warning("发送通知失败：%s", e)
        return False

NOTIFY_STATE_FILE = f"{MIHOMO_DIR}/notify_state.json"

def notify_site_name():
    return notify_format.site_label(read_env().get("SITE_NAME", ""))

def notify_event(level, subject, lines, key=None, send=None, site=None, state_file=None, now=None):
    """统一格式的通知：标题 "{图标} {站点名} · {主题}"，正文一行一个事实。
    level: ok / warn / fail / info；key 不为空时对重复失败去重（见 notify_format.py）。
    返回实际发送的 (title, body)，被去重抑制时返回 None。"""
    prepared = notify_format.prepare(level, subject, lines, site=notify_site_name() if site is None else site,
                                     key=key, state_file=state_file or NOTIFY_STATE_FILE, now=now)
    if prepared is None:
        return None
    (send or run_notify)(*prepared)
    return prepared

def finalize_panel_auto_update(path=None, now=None, notify=run_notify):
    """自动更新升级面板后旧进程会被替换；新面板启动时在这里把 started 记成最终结果。"""
    now = int(now if now is not None else time.time())
    state = read_auto_update_state(path)
    item = state["items"].get("panel") or {}
    if item.get("last_result") != "started":
        return None
    target = panel_version_tuple(item.get("to"))
    current = panel_version_tuple(PANEL_VERSION)
    from_version = "v" + str(item.get("from") or "?").lstrip("v")
    to_version = "v" + str(item.get("to") or "?").lstrip("v")
    if target and current and current >= target:
        result, message = "updated", f"面板已升级到 v{PANEL_VERSION}"
        notice = ("ok", "管理面板已更新", [f"{from_version} → v{PANEL_VERSION}", "新版本已启动，面板可以正常访问"])
    elif now - int(item.get("started_at") or item.get("last_check") or 0) > AUTO_UPDATE_PANEL_PENDING_SECONDS:
        result, message = "failed", f"升级到 {to_version} 后面板仍是 v{PANEL_VERSION}，可能已回滚"
        notice = ("fail", "管理面板更新失败，已回滚", [f"{from_version} → {to_version}",
                  f"升级 10 分钟后仍在运行 v{PANEL_VERSION}，新版本没有启动成功",
                  f"当前继续使用 v{PANEL_VERSION}"])
    else:
        return None
    item.update({"last_result": result, "message": message, "finished_at": now})
    state["items"]["panel"] = item
    write_auto_update_state(state, path)
    try:
        with open(AUTO_UPDATE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [panel] {message}\n")
    except OSError:
        pass
    if notify:
        notify_event(*notice, send=notify)
    return result

def startup_auto_update_hooks():
    for hook in (finalize_panel_auto_update, ensure_auto_update_cron):
        try:
            hook()
        except Exception as e:
            log.warning("自动更新启动检查失败 (%s)：%s", hook.__name__, e)

def server_timezone_info(now=None):
    local = time.localtime(now if now is not None else time.time())
    offset_minutes = int(local.tm_gmtoff // 60)
    name = ""
    try:
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            name = real.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    sign = "+" if offset_minutes >= 0 else "-"
    return {
        "name": name or local.tm_zone or "UTC",
        "abbr": local.tm_zone or "",
        "offset_minutes": offset_minutes,
        "offset_text": f"UTC{sign}{abs(offset_minutes) // 60:02d}:{abs(offset_minutes) % 60:02d}",
        "now": time.strftime("%Y-%m-%d %H:%M", local),
    }

def dashboard_updated_at():
    """Dashboard（external-ui 目录，默认 /etc/mihomo/ui）index.html 的修改时间；没有返回 0。"""
    value = config_value("external-ui") or "ui"
    target = value if os.path.isabs(value) else os.path.join(MIHOMO_DIR, value)
    try:
        return int(os.path.getmtime(os.path.join(target, "index.html")))
    except OSError:
        return 0

def auto_update_status_payload():
    version_ok, version = mihomo_api_get("/version", timeout=2)
    return {
        "settings": auto_update_settings(),
        "state": read_auto_update_state(),
        "running": auto_update_lock_busy(),
        "timezone": server_timezone_info(),
        "panel_version": PANEL_VERSION,
        "core_version": version.get("version", "") if version_ok and isinstance(version, dict) else "",
        "ui_updated_at": dashboard_updated_at(),
        "server_time": int(time.time()),
        "labels": {"items": AUTO_UPDATE_ITEM_LABELS, "results": AUTO_UPDATE_RESULT_LABELS},
    }

def run_auto_update_dry_run():
    """同步跑 auto_update.py --dry-run --json，只检查不安装。返回 (ok, 报告字典或错误说明)。"""
    if not os.path.exists(AUTO_UPDATE_SCRIPT):
        return False, f"缺少 {AUTO_UPDATE_SCRIPT}"
    try:
        result = subprocess.run([sys.executable or "python3", AUTO_UPDATE_SCRIPT, "--dry-run", "--json", "--manual"],
                                capture_output=True, text=True, timeout=AUTO_UPDATE_DRY_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"检查超时（{AUTO_UPDATE_DRY_RUN_TIMEOUT} 秒）"
    except Exception as e:
        return False, f"检查无法执行：{e}"
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    try:
        report = json.loads(lines[-1]) if lines else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        return False, ((result.stdout or "") + (result.stderr or "")).strip()[-2000:] or f"检查失败（退出码 {result.returncode}）"
    return bool(report.get("success", result.returncode == 0)), report

def spawn_auto_update():
    """后台启动真实更新。优先 systemd-run 放进独立的临时服务：面板升级会重启 mihomo-manager，
    systemd 默认会把本服务 cgroup 里的子进程一起杀掉。"""
    if not os.path.exists(AUTO_UPDATE_SCRIPT):
        return False, f"缺少 {AUTO_UPDATE_SCRIPT}"
    command = [sys.executable or "python3", AUTO_UPDATE_SCRIPT, "--manual"]
    if shutil.which("systemd-run"):
        unit = f"mihomo-auto-update-{int(time.time())}"
        ok, output = run_args(["systemd-run", "--quiet", "--collect", f"--unit={unit}"] + command, timeout=20)
        if ok:
            return True, "已在后台开始更新"
        log.warning("systemd-run 启动自动更新失败，改用普通后台进程：%s", output)
    try:
        with open(AUTO_UPDATE_LOG, "a", encoding="utf-8") as err:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                             close_fds=True, start_new_session=True)
    except Exception as e:
        return False, f"无法启动后台更新：{e}"
    return True, "已在后台开始更新"

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
            return render_template('login.html', error=f"登录失败次数过多，请 {wait} 秒后再试", site_name=read_env().get("SITE_NAME", "")), 429
        if check_creds(request.form.get('username'), request.form.get('password')):
            clear_login_failures(ip)
            session['logged_in'] = True
            session.permanent = True
            return redirect('/')
        failures = record_login_failure(ip)
        log.warning("登录失败 ip=%s 连续失败 %d 次", ip, failures)
        return render_template('login.html', error="用户名或密码错误", site_name=read_env().get("SITE_NAME", "")), 401

    if session.get('logged_in'):
        return redirect('/')
    return render_template('login.html', site_name=read_env().get("SITE_NAME", ""))

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/')
def index():
    if session.get('logged_in'):
        # 页面里写死自己的版本号，轮询发现服务端版本变了就提示刷新（旧标签页还跑着旧 JS）
        return render_template('index.html', panel_version=PANEL_VERSION, site_name=read_env().get("SITE_NAME", ""))
    return redirect('/login')

@app.route('/api/site-info')
def api_site_info():
    """不需要登录：只给其他站点面板的“站点切换”菜单显示名字用，不含任何其他信息。"""
    resp = jsonify({"site_name": read_env().get("SITE_NAME", "")})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

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

NOTIFY_LOG_FILE = "/var/log/mihomo-notify.log"
NOTIFY_LOG_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (Webhook|Telegram) (sent|failed) title=(.*?) (?:exit=(\d+) )?http=(\d+)(.*)$")

def recent_notifications(limit=15, path=None):
    """通知中心的“最近通知”：解析 notify.sh 写的日志，最新的在前。时间是 notify.sh 写的北京时间。"""
    text = read_recent_log_lines(path or NOTIFY_LOG_FILE, 400, tail_bytes=64 * 1024) if os.path.exists(path or NOTIFY_LOG_FILE) else ""
    items = []
    for line in text.splitlines():
        match = NOTIFY_LOG_LINE_RE.match(line.strip())
        if not match:
            continue
        stamp, channel, state, title, exit_code, http, rest = match.groups()
        error = ""
        if state == "failed":
            detail = re.search(r"error=(.*?)(?: response=|$)", rest or "")
            error = (detail.group(1).strip() if detail else "") or f"exit={exit_code or '?'} http={http}"
        items.append({"at": stamp, "channel": channel, "ok": state == "sent", "title": title[:200], "http": http, "error": error[:200]})
    return items[-limit:][::-1]

@app.route('/api/notify-log')
@login_required
def api_notify_log():
    return jsonify({"items": recent_notifications()})

@app.route('/api/devices')
@login_required
def api_devices():
    return jsonify(devices_snapshot())

@app.route('/api/devices/reset', methods=['POST'])
@login_required
def api_devices_reset():
    try:
        reset_device_totals()
    except Exception as e:
        return jsonify({"success": False, "message": f"清零失败：{e}"})
    return jsonify({"success": True, "message": "设备流量统计已清零"})

@app.route('/api/devices/<ip>/note', methods=['POST'])
@login_required
def api_device_note(ip):
    note = json_body().get("note", "")
    if not isinstance(note, str):
        return jsonify({"success": False, "message": "备注必须是文本"}), 400
    try:
        ok, message = set_device_note(ip, note)
    except Exception as e:
        return jsonify({"success": False, "message": f"保存备注失败：{e}"})
    return jsonify({"success": ok, "message": message})

@app.route('/api/devices/<ip>/apps')
@login_required
def api_device_apps(ip):
    payload, status = device_apps(ip)
    return jsonify(payload), status

@app.route('/api/ikuai-settings', methods=['GET', 'POST'])
@login_required
def api_ikuai_settings():
    if request.method == 'GET':
        return jsonify(ikuai_settings_payload())
    try:
        ok, message = save_ikuai_settings(json_body())
    except Exception as e:
        ok, message = False, f"保存失败：{e}"
    return jsonify({"success": ok, "message": message, **ikuai_settings_payload()})

@app.route('/api/ikuai-test', methods=['POST'])
@login_required
def api_ikuai_test():
    ok, message = test_ikuai_connection(json_body())
    return jsonify({"success": ok, "message": message})

@app.route('/api/panel-upgrade-source')
@login_required
def api_panel_upgrade_source():
    return jsonify(panel_upgrade_state())

@app.route('/api/auto-update')
@login_required
def api_auto_update():
    return jsonify(auto_update_status_payload())

@app.route('/api/auto-update/settings', methods=['POST'])
@login_required
def api_auto_update_settings():
    ok, message, settings = validate_auto_update_settings(json_body())
    if not ok:
        return jsonify({"success": False, "message": message})
    if not CONFIG_LOCK.acquire(blocking=False):
        return jsonify({"success": False, "message": BUSY_MESSAGE})
    try:
        write_env(auto_update_env_updates(settings))
        cron_ok, cron_message = apply_auto_update_cron(settings)
    finally:
        CONFIG_LOCK.release()
    if not cron_ok:
        return jsonify({"success": False, "message": "设置已写入 .env，但定时任务更新失败：\n" + cron_message})
    state = f"每天 {settings['time']}（服务器时间）自动检查" if settings["enabled"] else "已关闭自动更新"
    return jsonify({"success": True, "message": f"自动更新设置已保存：{state}", "settings": settings})

@app.route('/api/auto-update/check', methods=['POST'])
@login_required
def api_auto_update_check():
    if auto_update_lock_busy():
        return jsonify({"success": False, "message": "自动更新正在运行中，请稍后再检查。"})
    ok, report = run_auto_update_dry_run()
    if not isinstance(report, dict):
        return jsonify({"success": False, "message": report})
    return jsonify({"success": ok, "message": "检查完成（未安装任何东西）" if ok else "检查未全部成功", "report": report})

@app.route('/api/auto-update/run', methods=['POST'])
@login_required
def api_auto_update_run():
    # 和配置写入/重启互斥：正在改配置时不启动更新
    if not CONFIG_LOCK.acquire(blocking=False):
        return jsonify({"success": False, "message": BUSY_MESSAGE})
    try:
        if auto_update_lock_busy():
            return jsonify({"success": False, "message": "自动更新已经在运行中。"})
        started_at = int(time.time())
        ok, message = spawn_auto_update()
    finally:
        CONFIG_LOCK.release()
    # started_at 用服务器时钟，前端拿它判断“这次运行结束了没有”，不受浏览器时钟偏差影响
    return jsonify({"success": ok, "message": message, "started_at": started_at})

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
        'test_notify': ['bash', f'{SCRIPT_DIR}/notify.sh', '--event', 'info', '通知测试', '通知通道正常']
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
        if action == 'test_notify':
            # notify.sh 发送失败或没开任何渠道时也是退出码 0，按它的输出判断
            s = s and "已发送" in m and "发送失败" not in m
        return jsonify({"success": s, "message": m})
    finally:
        CONFIG_LOCK.release()

CONFIG_UNCHANGED_MESSAGE = "配置内容没有变化，未重启"

def config_text_key(text):
    """比较配置是否变化用：只统一换行符和末尾换行，不重排 YAML。"""
    text = str(text or "").replace("\r\n", "\n")
    return text[:-1] if text.endswith("\n") else text

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
            # 内容和当前 config.yaml 一样（只忽略换行符差异和末尾换行）就什么都不做，也不重启/热加载
            if os.path.exists(CONFIG_FILE) and config_text_key(content) == config_text_key(read_config_text()):
                return jsonify({"success": True, "unchanged": True, "message": CONFIG_UNCHANGED_MESSAGE})
            tmp_file = write_tmp_config(content, ".webcheck")
            ok, message = validate_config(tmp_file)
            if not ok:
                return jsonify({"success": False, "message": "配置校验失败，未保存：\n" + message})
            if os.path.exists(CONFIG_FILE):
                os.makedirs(BACKUP_DIR, exist_ok=True)
                shutil.copy2(CONFIG_FILE, f"{BACKUP_DIR}/config_{time.strftime('%Y%m%d%H%M%S')}.yaml")
                prune_config_backups()
            old_text = read_config_text() if os.path.exists(CONFIG_FILE) else ""
            os.replace(tmp_file, CONFIG_FILE)
            tmp_file = None
            # 后端知道新旧内容：没碰网络结构就热加载（不断连接），否则才重启
            ok, message, action = apply_config_change(old_text, content)
            if not ok:
                message = "配置已保存，但" + message
            return jsonify({"success": ok, "message": message, "action": action})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})
        finally:
            if tmp_file:
                remove_quietly(tmp_file)
            CONFIG_LOCK.release()

# 运行日志页可以看的日志（只允许这几个文件）
LOG_SOURCES = {
    "core": LOG_FILE,
    "subscription": "/var/log/mihomo-subscription.log",
    "geo": "/var/log/mihomo-geo.log",
    "auto_update": "/var/log/mihomo-auto-update.log",
    "notify": "/var/log/mihomo-notify.log",
}
LOG_VIEW_LINES = 300
LOG_SEARCH_TAIL_BYTES = 4 * 1024 * 1024

def core_log_level(line):
    match = re.search(r"level=([a-zA-Z]+)", line)
    level = match.group(1).lower() if match else ""
    return {"warning": "warn", "fatal": "error"}.get(level, level)

def read_log_view(source="core", level="", query="", limit=LOG_VIEW_LINES):
    """返回 (日志文本, 说明)。筛选和搜索在服务端做，范围是日志末尾 4MB，而不是只在最后 100 行里找。"""
    path = LOG_SOURCES.get(source)
    if not path:
        return "", "未知日志"
    if not os.path.exists(path):
        return "", "日志还没有生成"
    filtering = bool(level or query)
    text = read_recent_log_lines(path, 200000 if filtering else limit, tail_bytes=LOG_SEARCH_TAIL_BYTES if filtering else LOG_TAIL_BYTES)
    lines = text.splitlines()
    if level and source == "core":
        lines = [line for line in lines if core_log_level(line) == level]
    if query:
        needle = query.lower()
        lines = [line for line in lines if needle in line.lower()]
    total = len(lines)
    lines = lines[-limit:]
    note = f"共找到 {total} 行，显示最新 {len(lines)} 行" if filtering and total > len(lines) else ""
    return "\n".join(lines), note

@app.route('/api/logs')
@login_required
def get_logs():
    source = request.args.get("source", "core")
    level = request.args.get("level", "")
    level = level if level in ("error", "warn", "info", "debug") else ""
    query = str(request.args.get("q", ""))[:100].strip()
    logs, note = read_log_view(source, level, query)
    return jsonify({"logs": logs, "note": note, "source": source})

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
        return jsonify({**read_sync_settings(), "load_mode": sync_load_mode_label()})
    ok, message = write_sync_settings(json_body())
    return jsonify({"success": ok, "message": message, **read_sync_settings(), "load_mode": sync_load_mode_label()})

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
        saved, save_message, action = save_rule_content(rule_id, content)
        if not saved:
            return jsonify({"success": False, "message": save_message})
        if action == "restart":
            ok, message = restart_mihomo()
            if ok:
                message = "规则已保存并重启 mihomo"
                if save_message != "已写入规则集配置（本次需重启 mihomo）":
                    message += "（" + save_message + "）"
            else:
                message = "规则已保存，但 mihomo 重启失败：\n" + message
        elif action == "unchanged":
            ok, message = True, save_message
        else:
            ok, message = True, "规则已保存，" + save_message
    finally:
        CONFIG_LOCK.release()
    sync_job = None
    if ok:
        # 内容没变也照样推送：其他节点可能还是旧规则，已一致的节点会自己跳过。
        # 同步在后台线程里进行，不持有 CONFIG_LOCK；前端拿 sync_job 轮询结果
        sync_job, sync_message = start_broadcast(rule_id, content)
        if sync_job:
            message = message + "；" + sync_message
        elif sync_message:
            message = message + "\n\n" + sync_message
    return jsonify({"success": ok, "message": message, "sync_job": sync_job})


@app.route("/api/rule-sync-jobs/<job_id>")
@login_required
def api_rule_sync_job(job_id):
    # job_id 为 latest 时返回最近一次同步任务；还没有任务时 job 为 null
    job = find_sync_job(job_id)
    if job is None and job_id != "latest":
        return jsonify({"success": False, "message": "同步任务不存在或已过期"}), 404
    return jsonify({"success": True, "job": job})

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def handle_settings():
    if request.method == 'GET':
        e = read_env()
        sub_url_airport = e.get('SUB_URL_AIRPORT', '').replace('\\n', '\n')
        sub_schedule = cron_to_mode(e.get('CRON_SUB_SCHED', '0 5 * * *'), '05:00')
        geo_schedule = cron_to_mode(e.get('CRON_GEO_SCHED', '0 4 * * *'), '04:00')
        local_effective, local_auto, _ = effective_local_networks(e.get('LOCAL_CIDR', ''))

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
            "site_name": e.get('SITE_NAME', ''),
            "site_name_default": notify_format.DEFAULT_SITE_NAME,
            
            "local_cidr": e.get('LOCAL_CIDR', ''),
            "local_networks_effective": local_effective,
            "local_networks_auto": local_auto,
            "cron_sub_enabled": e.get('CRON_SUB_ENABLED') == 'true',
            "cron_sub_sched": e.get('CRON_SUB_SCHED', '0 5 * * *'), 
            "cron_sub_schedule": e.get('CRON_SUB_SCHED', '0 5 * * *'),
            "cron_sub_mode": e.get('CRON_SUB_MODE', sub_schedule['mode']),
            "cron_sub_time": e.get('CRON_SUB_TIME', sub_schedule['time']),
            "cron_geo_enabled": e.get('CRON_GEO_ENABLED') == 'true',
            "cron_geo_sched": e.get('CRON_GEO_SCHED', '0 4 * * *'),
            "cron_geo_schedule": e.get('CRON_GEO_SCHED', '0 4 * * *'),
            "cron_geo_mode": e.get('CRON_GEO_MODE', geo_schedule['mode']),
            "cron_geo_time": e.get('CRON_GEO_TIME', geo_schedule['time']),
            # 页面按浏览器本地时间显示/填写定时任务，用它换算
            "timezone": server_timezone_info(),
        })

    if request.method == 'POST':
        d = json_body()
        current = read_env()
        # 只改请求里带了的键：没带的保持 .env 原值，老页面/设置没加载完的页面不会把它们清空
        updates = {}
        if 'config_mode' in d:
            updates["CONFIG_MODE"] = d.get('config_mode') if d.get('config_mode') in ('raw', 'airport') else 'airport'
        if 'sub_url_raw' in d:
            updates["SUB_URL_RAW"] = str(d.get('sub_url_raw') or '')
        if 'sub_url_airport' in d:
            raw_airport = d.get('sub_url_airport') or ''
            if isinstance(raw_airport, list):
                raw_airport = "\n".join(str(item) for item in raw_airport)
            updates["SUB_URL_AIRPORT"] = str(raw_airport).replace('\n', '\\n')

        if 'notify_api' in d:
            updates["NOTIFY_API"] = str(is_true(d.get('notify_api'))).lower()
        if 'api_url' in d or 'notify_api_url' in d:
            updates["NOTIFY_API_URL"] = d.get('api_url') or d.get('notify_api_url') or ''
        # 只有通知页带了站点名时才改，别的调用方不会把它清掉
        if 'site_name' in d:
            ok, site_name, site_error = notify_format.validate_site_name(d.get('site_name'))
            if not ok:
                return jsonify({"success": False, "message": site_error}), 400
            updates["SITE_NAME"] = site_name

        if 'local_cidr' in d:
            networks, cidr_error = normalize_local_networks(d.get('local_cidr'))
            if cidr_error:
                return jsonify({"success": False, "message": "本地直连网段：" + cidr_error}), 400
            # 规范化后逗号分隔存；空字符串 = 自动识别本机网段
            updates["LOCAL_CIDR"] = ",".join(networks)

        cron_jobs = []
        for kind, job_id, default_cron, default_time, script, log_path in (
            ("sub", "# JOB_SUB", "0 5 * * *", "05:00", "update_subscription.sh", SUBSCRIPTION_LOG),
            ("geo", "# JOB_GEO", "0 4 * * *", "04:00", "update_geo.sh", GEO_LOG),
        ):
            keys = [f"cron_{kind}_{name}" for name in ("enabled", "mode", "time", "sched", "schedule")]
            if not any(key in d for key in keys):
                continue
            prefix = f"CRON_{kind.upper()}_"
            enabled = is_true(d[f"cron_{kind}_enabled"]) if f"cron_{kind}_enabled" in d else current.get(prefix + "ENABLED") == 'true'
            mode = d.get(f"cron_{kind}_mode", current.get(prefix + "MODE", "daily"))
            time_value = d.get(f"cron_{kind}_time", current.get(prefix + "TIME", default_time))
            advanced = d.get(f"cron_{kind}_sched") or d.get(f"cron_{kind}_schedule") or current.get(prefix + "SCHED") or default_cron
            schedule = build_schedule(mode, time_value, advanced, default_cron)
            updates.update({
                prefix + "ENABLED": str(enabled).lower(),
                prefix + "SCHED": schedule,
                prefix + "MODE": mode,
                prefix + "TIME": time_value,
            })
            cron_jobs.append((job_id, schedule, script, log_path, enabled))

        if updates:
            write_env(updates)

        cron_errors = []
        for job_id, schedule, script, log_path, enabled in cron_jobs:
            # 输出进日志而不是 /dev/null，不然用户没法知道定时任务到底跑没跑
            # (update_subscription.sh >> {SUBSCRIPTION_LOG} 2>&1 / update_geo.sh >> {GEO_LOG} 2>&1)
            ok, message = update_cron(job_id, schedule, f"bash {SCRIPT_DIR}/{script} >> {log_path} 2>&1", enabled)
            if not ok:
                cron_errors.append(message)
        # 顺带确认自动更新的定时任务和 .env 一致（升级上来的老面板也能补上）
        auto_ok, auto_message = ensure_auto_update_cron()
        if not auto_ok:
            cron_errors.append(auto_message)
        if cron_errors:
            return jsonify({"success": False, "message": "设置已写入 .env，但定时任务更新失败：\n" + "\n".join(cron_errors)})

        return jsonify({"success": True, "message": "配置已成功保存！"})

if __name__ == '__main__':
    env = read_env()
    try:
        port = int(env.get('WEB_PORT', 7838))
    except Exception:
        port = 7838
    # systemd 停服务发 SIGTERM，默认处理是直接退出不跑 atexit；转成 SystemExit 让采样线程把统计落盘
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # 自动更新：补齐/修正 cron 行；上一次自动升级面板后在这里记录最终结果
    startup_auto_update_hooks()
    start_device_sampler()
    app.run(host='0.0.0.0', port=port)
