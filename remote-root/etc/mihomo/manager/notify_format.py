#!/usr/bin/env python3
"""通知标题/正文的唯一实现（只用标准库，scripts/notify.sh --event 和面板 app.notify_event 都用它）。

标题：{图标} {站点名} · {主题}，例如 "✅ 联通 · mihomo 内核已更新"
  ✅ ok   成功 / 已恢复
  ⚠️ warn 出了问题但还在正常工作（旧配置 / 旧版本继续用）
  ❌ fail 坏了或已回滚
  🔔 info 提示 / 测试
正文：一行一个事实的纯文本（Webhook 转发到微信不渲染 Markdown），时间由 notify.sh 追加。

重复失败去重（传 key 时）：状态存在 notify_state.json，
  - 第一次失败（或失败级别变了）→ 发；
  - 仍在失败 → 距上次提醒满 3 天才再发一次，并加一行“持续失败第 N 天”；
  - 失败后恢复（ok/info + 同一个 key）→ 发一次“已恢复”；本来就正常 → 不发。

命令行（notify.sh 调用）：
  notify_format.py --level ok|warn|fail|info --site 站点名 [--key K] [--state-file F] [--now 秒] 主题 [行 ...]
  输出：第一行 send 或 skip，第二行标题，其余是正文。
"""
import argparse
import json
import os
import sys
import tempfile
import time

LEVEL_ICONS = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "🔔"}
FAILING_LEVELS = ("warn", "fail")
DEFAULT_SITE_NAME = "mihomo 网关"
SITE_NAME_MAX = 20
SITE_NAME_FORBIDDEN = ("\n", "\r", "\t", "\"", "'", "`", "\\", "$")
MAX_LINES = 5  # 调用方最多 4 行，再留一行给“持续失败第 N 天”
REMIND_SECONDS = 3 * 86400
DAY = 86400
STATE_FILE = "/etc/mihomo/notify_state.json"


def validate_site_name(value):
    """返回 (ok, 清理后的值, 错误说明)。空值合法（= 用默认名）。"""
    text = str(value if value is not None else "").strip()
    if any(ch in text for ch in SITE_NAME_FORBIDDEN):
        return False, text, "站点名称不能包含换行、引号、反斜杠或 $ 符号"
    if len(text) > SITE_NAME_MAX:
        return False, text, f"站点名称最多 {SITE_NAME_MAX} 个字符"
    return True, text, ""


def site_label(value):
    ok, text, _ = validate_site_name(value)
    return text if ok and text else DEFAULT_SITE_NAME


def build_title(level, subject, site=None):
    icon = LEVEL_ICONS.get(level, LEVEL_ICONS["info"])
    subject = " ".join(str(subject or "").split()) or "通知"
    return f"{icon} {site_label(site)} · {subject}"


def build_body(lines):
    clean = [" ".join(str(line).split()) for line in (lines or []) if str(line or "").strip()]
    return "\n".join(clean[:MAX_LINES])


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".notify_state.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def dedupe(state, key, level, now):
    """返回 (是否发送, 附加行或 None)，并原地更新 state。"""
    entry = state.get(key) if isinstance(state.get(key), dict) else None
    failing = bool(entry and entry.get("failing"))
    if level in FAILING_LEVELS:
        if not failing or entry.get("level") != level:
            since = int(entry.get("since")) if failing and entry.get("since") else int(now)
            state[key] = {"failing": True, "level": level, "since": since, "last_notified": int(now)}
            extra = None
            if failing:
                extra = f"持续失败第 {int((now - since) // DAY) + 1} 天"
            return True, extra
        if now - int(entry.get("last_notified") or 0) >= REMIND_SECONDS:
            entry["last_notified"] = int(now)
            days = int((now - int(entry.get("since") or now)) // DAY) + 1
            return True, f"持续失败第 {days} 天"
        return False, None
    if failing:
        state.pop(key, None)
        return True, None
    return False, None


def prepare(level, subject, lines, site=None, key=None, state_file=None, now=None):
    """返回 (title, body)；被去重抑制时返回 None。"""
    if level not in LEVEL_ICONS:
        level = "info"
    lines = list(lines or [])
    if key:
        path = state_file or STATE_FILE
        now = time.time() if now is None else now
        state = load_state(path)
        send, extra = dedupe(state, str(key), level, now)
        save_state(path, state)
        if not send:
            return None
        if extra:
            lines = lines[:MAX_LINES - 1] + [extra]
    return build_title(level, subject, site), build_body(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--level", required=True, choices=sorted(LEVEL_ICONS))
    parser.add_argument("--site", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--state-file", default=os.environ.get("NOTIFY_STATE_FILE") or STATE_FILE)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("subject")
    parser.add_argument("lines", nargs="*")
    args = parser.parse_args(argv)
    now = args.now
    if now is None and os.environ.get("NOTIFY_NOW"):
        now = float(os.environ["NOTIFY_NOW"])
    result = prepare(args.level, args.subject, args.lines, site=args.site, key=args.key or None,
                     state_file=args.state_file, now=now)
    if result is None:
        sys.stdout.write("skip\n")
        return 0
    title, body = result
    sys.stdout.write(f"send\n{title}\n{body}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
