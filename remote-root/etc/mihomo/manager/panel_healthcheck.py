#!/usr/bin/env python3
"""Health probe started before mihomo-manager restarts into an upgraded panel.

The plan JSON is read once, into this process, before the old service is restarted.
A new panel that crashes, or never answers GET /login twice in a row, is rolled
back from the backup named in that plan. This file must stay standalone: importing
the new app.py is exactly what a bad upgrade can make impossible.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from urllib import request as urlrequest


def load_plan(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("健康检查计划必须是 JSON 对象")
    return data


def log_line(plan, message):
    text = time.strftime("%Y-%m-%d %H:%M:%S") + " " + message
    print(text, file=sys.stderr)
    path = str(plan.get("log_path") or "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def write_arm(plan):
    path = str(plan.get("armed_path") or "")
    if not path:
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("armed\n")
        handle.flush()
        os.fsync(handle.fileno())


def pid_gone(pid):
    if not isinstance(pid, int) or pid <= 0:
        return True
    return not os.path.exists(f"/proc/{pid}")


def wait_pid(plan, pid):
    timeout = float(plan.get("pid_wait", 25))
    interval = float(plan.get("pid_interval", 0.5))
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if pid_gone(pid):
            return True
        time.sleep(max(0.05, interval))
    return pid_gone(pid)


def probe_once(url):
    try:
        req = urlrequest.Request(url, headers={"User-Agent": "mihomo-panel-healthcheck"})
        with urlrequest.urlopen(req, timeout=3) as resp:
            body = resp.read(65536)
            status = getattr(resp, "status", None) or resp.getcode()
    except Exception:
        return False
    return int(status) == 200 and b'name="password"' in body and b"/login" in body


def safe_target(target, allowed):
    if not isinstance(target, str) or target not in allowed:
        return False
    if not target.startswith("/") or ".." in target.split("/"):
        return False
    return True


def restore(plan):
    backup_root = str(plan.get("backup_root") or "")
    if not backup_root or not os.path.isdir(backup_root):
        raise ValueError("备份目录不存在")
    allowed = set(plan.get("allowed_targets") or [])
    manifest_path = os.path.join(backup_root, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, list):
        raise ValueError("备份清单格式不对")
    restored = 0
    for item in manifest:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if not safe_target(target, allowed):
            log_line(plan, f"跳过不在允许列表中的恢复目标：{target}")
            continue
        backup_path = os.path.join(backup_root, target.lstrip("/"))
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        elif os.path.lexists(target):
            os.remove(target)
        if not item.get("existed"):
            restored += 1
            continue
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if item.get("kind") == "dir":
            shutil.copytree(backup_path, target, symlinks=False)
        else:
            shutil.copy2(backup_path, target)
        restored += 1
    if restored <= 0:
        raise ValueError("没有恢复任何文件")
    return restored


def systemctl(plan, *args):
    if not plan.get("control_service"):
        return
    try:
        subprocess.run(["systemctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except Exception as exc:
        log_line(plan, "systemctl " + " ".join(args) + f" 失败：{exc}")


def run(plan):
    try:
        old_pid = int(plan.get("old_pid") or 0)
    except (TypeError, ValueError):
        old_pid = 0
    if not wait_pid(plan, old_pid) and plan.get("control_service"):
        log_line(plan, "旧进程仍在，尝试重启 mihomo-manager")
        systemctl(plan, "restart", "mihomo-manager")
        wait_pid(plan, old_pid)
    initial = float(plan.get("initial_wait") or 0)
    if initial > 0:
        time.sleep(initial)
    url = str(plan.get("url") or "")
    attempts = int(plan.get("attempts") or 12)
    interval = plan.get("interval")
    interval = 2.0 if interval is None else float(interval)
    streak = 0
    for _ in range(max(1, attempts)):
        if url and probe_once(url):
            streak += 1
            if streak >= 2:
                log_line(plan, "/login 连续两次响应，保留新版本")
                return 0
        else:
            streak = 0
        if interval > 0:
            time.sleep(interval)
    log_line(plan, "/login 未通过健康检查，开始回滚")
    if plan.get("control_service"):
        systemctl(plan, "stop", "mihomo-manager")
    try:
        restore(plan)
    except Exception as exc:
        log_line(plan, f"回滚失败：{exc}")
        return 1
    if plan.get("control_service"):
        systemctl(plan, "daemon-reload")
        systemctl(plan, "start", "mihomo-manager")
    log_line(plan, "已回滚到升级前的备份")
    return 1


def main(argv):
    if len(argv) != 2:
        print("usage: panel_healthcheck.py PLAN.json", file=sys.stderr)
        return 2
    plan = load_plan(argv[1])
    # 先把计划读进内存并留下 armed 标记，再等旧进程退出。这样重启后的新代码
    # 改不了这次检查要用的备份路径。
    write_arm(plan)
    return run(plan)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
