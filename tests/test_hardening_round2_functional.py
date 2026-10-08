"""第二轮加固的功能测试：真的执行 app.py 里的函数（Flask 用桩，/etc/mihomo 指到临时目录）。

模式和 test_rule_sync_render.py 一样。覆盖 .env 原子写入与权限、无默认账号、
日志只读尾部、配置锁、备份裁剪、GitHub 直连优先、crontab 失败上报。
"""
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
TEMPLATE = ROOT / "remote-root" / "etc" / "mihomo" / "templates" / "default.yaml"


def load_app(tmp_dir):
    flask = types.ModuleType("flask")

    class FakeFlask:
        permanent_session_lifetime = None
        secret_key = None

        def __init__(self, *args, **kwargs):
            self.config = {}

        def route(self, *args, **kwargs):
            return lambda func: func

    for name in ("render_template", "request", "jsonify", "Response", "redirect", "session"):
        setattr(flask, name, None)
    flask.Flask = FakeFlask
    sys.modules["flask"] = flask

    source = APP.read_text(encoding="utf-8").replace('MIHOMO_DIR = "/etc/mihomo"', f'MIHOMO_DIR = "{tmp_dir}"')
    module = types.ModuleType("mihomo_app_under_test_round2")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


class HardeningFunctionalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = load_app(self.tmp.name)
        for key in ("WEB_USER", "WEB_SECRET", "WEB_SESSION_SECRET"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()

    # --- .env ---
    def test_write_env_is_private_atomic_and_preserves_other_lines(self):
        env_file = self.dir / ".env"
        self.assertTrue(env_file.exists(), "导入时应已生成 WEB_SESSION_SECRET")
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

        self.app.write_env({"FOO": "a b'c", "BAR": "x"})
        self.app.write_env({"BAR": "y"})
        env = self.app.read_env()
        self.assertEqual(env["FOO"], "a b'c")
        self.assertEqual(env["BAR"], "y")
        self.assertIn("WEB_SESSION_SECRET", env)
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.dir.glob(".env.*.tmp")], [], "不能留下临时文件")

    def test_app_config_sets_cookie_flags_and_body_limit(self):
        self.assertEqual(self.app.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(self.app.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(self.app.app.config["MAX_CONTENT_LENGTH"], 1024 * 1024)

    # --- 登录 ---
    def test_check_creds_refuses_when_no_account_configured(self):
        self.assertFalse(self.app.check_creds("admin", "admin"))
        self.app.write_env({"WEB_USER": "ops", "WEB_SECRET": "s3cret"})
        self.assertTrue(self.app.check_creds("ops", "s3cret"))
        self.assertFalse(self.app.check_creds("ops", "wrong"))
        self.assertFalse(self.app.check_creds("admin", "admin"))

    def test_rotate_session_secret_changes_key(self):
        before = self.app.app.secret_key
        self.app.rotate_session_secret()
        self.assertNotEqual(before, self.app.app.secret_key)
        self.assertEqual(self.app.read_env()["WEB_SESSION_SECRET"], self.app.app.secret_key)

    def test_login_throttle_locks_after_max_failures(self):
        ip = "203.0.113.9"
        for _ in range(self.app.LOGIN_MAX_FAILURES - 1):
            self.app.record_login_failure(ip)
        self.assertEqual(self.app.login_locked(ip), 0)
        self.app.record_login_failure(ip)
        self.assertGreater(self.app.login_locked(ip), 0)
        self.app.clear_login_failures(ip)
        self.assertEqual(self.app.login_locked(ip), 0)

    # --- 日志 ---
    def test_read_recent_log_lines_reads_only_tail(self):
        log_path = self.dir / "mihomo.log"
        lines = [f"line {i:06d} level=info\n" for i in range(20000)]
        log_path.write_text("".join(lines), encoding="utf-8")
        self.assertGreater(log_path.stat().st_size, 256 * 1024)

        out = self.app.read_recent_log_lines(str(log_path), 5)
        self.assertEqual(out, "".join(lines[-5:]))

        # tail_bytes 很小且落在行中间时，第一行半截要被丢掉
        out = self.app.read_recent_log_lines(str(log_path), 100, tail_bytes=100)
        self.assertTrue(all(l.startswith("line ") for l in out.splitlines()))
        self.assertLess(len(out.splitlines()), 100)

        self.assertTrue(self.app.read_recent_log_lines(str(self.dir / "missing.log"), 5))

    # --- 并发锁 ---
    def test_update_sync_block_reports_busy_when_lock_held_elsewhere(self):
        (self.dir / "config.yaml").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        held = threading.Event()
        release = threading.Event()

        def holder():
            with self.app.CONFIG_LOCK:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        try:
            ok, message, _action = self.app.update_mihomo_sync_block({"force-cn": "a.cn\n", "force-nocn": ""})
            self.assertFalse(ok)
            self.assertEqual(message, self.app.BUSY_MESSAGE)
            self.assertFalse(self.app.apply_synced_rules({"force-cn": "a.cn\n"})[0])
        finally:
            release.set()
            thread.join(5)

        ok, message, action = self.app.update_mihomo_sync_block({"force-cn": "a.cn\n", "force-nocn": ""})
        self.assertTrue(ok, message)
        self.assertEqual(action, "restart")
        self.assertIn(self.app.RULE_PROVIDERS_BEGIN, (self.dir / "config.yaml").read_text(encoding="utf-8"))
        self.assertIn("+.a.cn", (self.dir / "rules" / "mosctl-force-cn.yaml").read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in self.dir.glob("config.yaml.*")], [], "校验用的临时文件必须清掉")

    # --- 备份裁剪 ---
    def test_prune_config_backups_keeps_newest_n(self):
        backup_dir = self.dir / "backup"
        backup_dir.mkdir()
        names = [f"config_{i:02d}.yaml" for i in range(6)] + [f"config.before-rule-sync.{i:02d}.yaml" for i in range(6)]
        now = time.time()
        for index, name in enumerate(names):
            path = backup_dir / name
            path.write_text("x", encoding="utf-8")
            os.utime(path, (now - 1000 + index, now - 1000 + index))
        self.app.write_env({"BACKUP_KEEP_COUNT": "3"})
        self.assertEqual(self.app.backup_keep_count(), 3)
        self.app.prune_config_backups()
        remaining = sorted(p.name for p in backup_dir.iterdir())
        self.assertEqual(remaining, sorted(names[-3:]))

        self.app.write_env({"BACKUP_KEEP_COUNT": "garbage"})
        self.assertEqual(self.app.backup_keep_count(), self.app.DEFAULT_BACKUP_KEEP_COUNT)

    # --- GitHub 直连优先 ---
    def test_github_candidate_urls_direct_first_then_env_proxy(self):
        url = "https://github.com/anxiaoyang666/mihomo/archive/refs/heads/main.zip"
        self.app.write_env({"GH_PROXY": ""})
        self.assertEqual(self.app.github_candidate_urls(url), [url])
        self.app.write_env({"GH_PROXY": "https://mirror.example.com"})
        self.assertEqual(self.app.github_candidate_urls(url), [url, "https://mirror.example.com/" + url])
        self.app.write_env({"GH_PROXY": "not-a-url"})
        self.assertEqual(self.app.github_candidate_urls(url), [url])
        self.assertEqual(self.app.github_candidate_urls("https://api.github.com/x"), ["https://api.github.com/x"])

    # --- crontab 失败上报 ---
    def test_update_cron_returns_failure_when_crontab_rejects(self):
        calls = []

        class FakeSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(args, **kwargs):
                calls.append(args)
                if args == ["crontab", "-l"]:
                    return types.SimpleNamespace(returncode=0, stdout="0 1 * * * echo hi # OTHER\n", stderr="")
                return types.SimpleNamespace(returncode=1, stdout="", stderr="crontab: permission denied")

        real = self.app.subprocess
        self.app.subprocess = FakeSubprocess
        try:
            ok, message = self.app.update_cron("# JOB_SUB", "0 5 * * *", "bash x.sh", True)
        finally:
            self.app.subprocess = real
        self.assertFalse(ok)
        self.assertIn("permission denied", message)
        self.assertEqual(calls[-1], ["crontab", "-"])

    def test_validate_config_without_core_is_skipped(self):
        ok, message = self.app.validate_config(str(self.dir / "whatever.yaml"))
        self.assertTrue(ok)
        self.assertIn("跳过", message)


if __name__ == "__main__":
    unittest.main()
