#!/bin/bash
# envutil.sh - /etc/mihomo/.env 读写的唯一 shell 实现，供 CLI (/usr/bin/mihomo) 和各脚本 source。
#
#   source /etc/mihomo/scripts/envutil.sh
#   upsert_env KEY VALUE   # 新增或替换一个键：shlex 引用、0600 临时文件 + 原子替换
#   get_env KEY            # 打印一个键的值（不认识的行跳过；不会执行 .env 里的内容）
#
# install.sh 在脚本目录还没装好之前就要写 .env，所以它保留了一份同样逻辑的 env_upsert/env_get，
# 改这里的规则时记得同步 install.sh。面板 app.py 的 write_env/parse_env_line 是 Python 侧的对应实现。

ENV_FILE="${ENV_FILE:-/etc/mihomo/.env}"

upsert_env() {
    local key=$1
    local value=$2
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import os, pathlib, re, secrets, shlex, sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
    sys.exit(f"envutil: 非法的键名 {key!r}")
line = f"{key}={shlex.quote(value)}\n"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=")
for index, existing in enumerate(lines):
    if pattern.match(existing):
        lines[index] = line
        break
else:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(line)
# .env 里有密码：0600 临时文件 + 原子替换
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(f".env.{os.getpid()}.{secrets.token_hex(4)}.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(lines))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
except BaseException:
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise
PY
    chmod 600 "$ENV_FILE"
}

get_env() {
    local key=$1
    [ -f "$ENV_FILE" ] || return 0
    python3 - "$ENV_FILE" "$key" <<'PY'
import re, shlex, sys

path, key = sys.argv[1], sys.argv[2]
value = ""
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, _, raw = line.partition("=")
    name = name.strip()
    if name.startswith("export "):
        name = name[len("export "):].strip()
    if name != key:
        continue
    raw = raw.strip()
    if raw.startswith("$'"):
        # bash printf %q 的 $'...' 形式：只解常见转义
        match = re.match(r"\$'((?:[^'\\]|\\.)*)'", raw)
        body = match.group(1) if match else raw[2:].rstrip("'")
        table = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
        value = re.sub(r"\\(.)", lambda m: table.get(m.group(1), m.group(1)), body)
        continue
    try:
        parts = shlex.split(raw, comments=False, posix=True)
        value = parts[0] if parts else ""
    except ValueError:
        value = raw[1:-1] if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0] else raw
print(value, end="")
PY
}
