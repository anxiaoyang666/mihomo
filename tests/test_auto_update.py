"""auto_update.py 的功能测试：GitHub 版本筛选、不降级、配置校验失败中止、健康检查失败回滚、
面板 UI 失败恢复、单实例锁、cron 行增删、设置校验、面板最小提交天数。全部用假的系统/网络函数。"""
from pathlib import Path
import contextlib
import gzip
import io
import importlib.util
import json
import os
import tempfile
import types
import unittest

from test_rule_sync_render import ROOT, load_app

AUTO_UPDATE = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "auto_update.py"
DAY = 86400
NOW = 1_800_000_000

CONFIG = """\
mixed-port: 7890
external-controller: 127.0.0.1:9090
external-ui: ui
secret: s3cret
listeners:
  - {name: ss-in, type: shadowsocks, port: 7895, cipher: aes-128-gcm, password: x}
  - name: mixed-in
    type: mixed
    port: 7896
dns:
  enable: true
  listen: 0.0.0.0:1053
  enhanced-mode: fake-ip
rules:
  - MATCH,DIRECT
"""


def load_auto_update():
    spec = importlib.util.spec_from_file_location("mihomo_auto_update_under_test", AUTO_UPDATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def release(tag, age_days, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "published_at": iso(NOW - age_days * DAY)}


class FakeSystem:
    """假内核：二进制文件内容就是 "VERSION=vX.Y.Z"，-v 读文件；“运行中的进程”版本在 restart 时取当时的文件。"""

    def __init__(self, root, current="v1.19.30"):
        self.root = Path(root)
        self.core_bin = self.root / "usr-bin" / "mihomo-core"
        self.core_bin.parent.mkdir(parents=True)
        self.core_bin.write_text(f"VERSION={current}")
        self.running_version = current
        self.restarts = 0
        self.config_valid = True
        self.downloads = []
        self.download_payload = None  # None = 正常 gzip；bytes = 原样写入
        self.download_version = None
        self.dns_ok = True
        self.dns_fail_versions = set()
        self.listening = {7890, 7895, 7896, 9090}
        self.missing_ports_versions = set()
        self.service_ok = True
        self.notifications = []
        self.releases = []
        self.fetch_ok = True
        self.commits = None  # None = 提交 API 不可用
        self.fetched = []

    def run(self, args, timeout=60):
        if len(args) >= 2 and args[1] == "-v":
            path = Path(args[0])
            if not path.exists():
                return 127, "not found"
            text = path.read_text()
            return 0, f"Mihomo Meta {text.split('=', 1)[1]} linux amd64 with go1.24"
        if len(args) >= 2 and args[1] == "-t":
            return (0, "configuration test is successful") if self.config_valid else (1, "yaml: line 3: bad")
        if args[:3] == ["systemctl", "restart", "mihomo"]:
            self.restarts += 1
            self.running_version = self.core_bin.read_text().split("=", 1)[1]
            return 0, ""
        if args and args[0] == "bash":
            return 0, ""
        raise AssertionError(f"unexpected command {args}")

    def download(self, urls, output):
        self.downloads.append(urls)
        if self.download_payload is not None:
            Path(output).write_bytes(self.download_payload)
            return True, urls[0]
        tag = self.download_version or urls[0].split("/download/")[1].split("/")[0]
        with gzip.open(output, "wb") as f:
            f.write(f"VERSION={tag}".encode())
        return True, urls[0]

    def fetch_text(self, urls, timeout=20):
        self.fetched.append(urls)
        if "/commits?" in urls[0]:
            if self.commits is None:
                return False, "HTTP Error 403: rate limit exceeded", ""
            return True, json.dumps(self.commits), urls[0]
        if not self.fetch_ok:
            return False, "timed out", ""
        return True, json.dumps(self.releases), urls[0]

    def dns_probe(self, host, port, name):
        self.dns_args = (host, port, name)
        if not self.dns_ok or self.running_version in self.dns_fail_versions:
            return False, f"DNS {host}:{port} 查询 {name} 失败：timed out"
        return True, "ok"

    def listening_ports(self):
        if self.running_version in self.missing_ports_versions:
            return self.listening - {7895}
        return set(self.listening)


class AutoUpdateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = load_app(self.tmp.name)
        self.app.AUTO_UPDATE_LOG = str(self.dir / "auto-update.log")
        (self.dir / "config.yaml").write_text(CONFIG)
        self.au = load_auto_update()
        self.sys = FakeSystem(self.dir / "sys")
        self.clock = [NOW]
        self.upgrade_calls = []
        self.upgrade_result = (True, "ok", True)
        self.rt = self.make_rt()

    def tearDown(self):
        self.tmp.cleanup()

    def fake_sleep(self, seconds):
        self.clock[0] += seconds

    def fake_upgrade_panel(self):
        self.upgrade_calls.append(True)
        return self.upgrade_result

    def make_rt(self, **overrides):
        s = self.sys
        values = dict(
            core_bin=str(s.core_bin),
            log_file=self.app.AUTO_UPDATE_LOG,
            lock_path=str(self.dir / "auto-update.lock"),
            now=lambda: self.clock[0],
            sleep=self.fake_sleep,
            machine=lambda: "x86_64",
            run=s.run,
            fetch_text=s.fetch_text,
            download=s.download,
            service_active=lambda name: s.service_ok,
            controller_version=lambda: s.running_version,
            dns_probe=s.dns_probe,
            listening_ports=s.listening_ports,
            notify=lambda title, content: s.notifications.append((title, content)),
            upgrade_panel=self.fake_upgrade_panel,
            echo=lambda *_: None,
        )
        values.update(overrides)
        return self.au.Runtime(self.app, **values)

    def settings(self, **overrides):
        values = dict(self.app.AUTO_UPDATE_DEFAULTS)
        values.update(overrides)
        return values

    def state(self):
        return self.app.read_auto_update_state(self.rt.state_file)


class CoreReleasePickTest(AutoUpdateTestBase):
    def test_prerelease_draft_and_too_new_are_skipped_older_eligible_picked(self):
        au = self.au
        releases = [
            release("Prerelease-Alpha", 0, prerelease=True),
            release("v1.19.40", 10, prerelease=True),
            release("v1.19.39", 10, draft=True),
            release("v1.19.35", 1),           # 太新
            release("v1.19.34", 4),           # 满 3 天
            release("v1.19.33", 20),
        ]
        tag, newest, note = au.pick_core_release(releases, 3, NOW)
        self.assertEqual(tag, "v1.19.34")
        self.assertEqual(newest, "v1.19.35")
        self.assertIn("不足 3 天", note)

    def test_nothing_old_enough(self):
        tag, newest, note = self.au.pick_core_release([release("v1.19.35", 1)], 6, NOW)
        self.assertEqual(tag, "")
        self.assertEqual(newest, "v1.19.35")
        self.assertIn("不足 6 天", note)

    def test_zero_min_age_takes_newest(self):
        tag, _, _ = self.au.pick_core_release([release("v1.19.33", 9), release("v1.19.35", 0)], 0, NOW)
        self.assertEqual(tag, "v1.19.35")

    def test_asset_naming_matches_install_kernel(self):
        name, url = self.au.core_asset("v1.19.35", "x86_64")
        self.assertEqual(name, "mihomo-linux-amd64-compatible-v1.19.35.gz")
        self.assertEqual(url, "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.35/mihomo-linux-amd64-compatible-v1.19.35.gz")
        self.assertEqual(self.au.core_asset("v1.19.35", "aarch64")[0], "mihomo-linux-arm64-v1.19.35.gz")
        self.assertEqual(self.au.core_asset("v1.19.35", "mips"), ("", ""))


class CoreUpdateTest(AutoUpdateTestBase):
    def setUp(self):
        super().setUp()
        self.sys.releases = [release("v1.19.36", 1), release("v1.19.35", 5), release("v1.19.29", 60)]

    def core_text(self):
        return self.sys.core_bin.read_text()

    def test_successful_update_backs_up_replaces_and_checks_health(self):
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "updated", report["message"])
        self.assertEqual((report["from"], report["to"]), ("v1.19.30", "v1.19.35"))
        self.assertEqual(self.core_text(), "VERSION=v1.19.35")
        self.assertEqual(self.sys.restarts, 1)
        backups = os.listdir(self.dir / "backup")
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("mihomo-core.v1.19.30."))
        # DNS 监听 0.0.0.0:1053 → 查 127.0.0.1:1053
        self.assertEqual(self.sys.dns_args[:2], ("127.0.0.1", 1053))
        # 下载走 github_candidate_urls（第一条是官方直连）
        self.assertTrue(self.sys.downloads[0][0].startswith("https://github.com/MetaCubeX/mihomo/releases/download/v1.19.35/"))
        self.assertFalse(any(name.startswith(".auto-update.") for name in os.listdir(self.dir)), "临时目录要清理")

    def test_never_downgrade(self):
        self.sys.core_bin.write_text("VERSION=v1.19.40")
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "up_to_date")
        self.assertIn("不降级", report["message"])
        self.assertEqual(self.sys.downloads, [])
        self.assertEqual(self.core_text(), "VERSION=v1.19.40")

    def test_same_version_is_up_to_date(self):
        self.sys.core_bin.write_text("VERSION=v1.19.35")
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "up_to_date")
        self.assertEqual(self.sys.restarts, 0)

    def test_config_validation_failure_aborts_without_changes(self):
        self.sys.config_valid = False
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "failed")
        self.assertIn("校验当前配置失败", report["message"])
        self.assertEqual(self.core_text(), "VERSION=v1.19.30")
        self.assertEqual(self.sys.restarts, 0)
        self.assertFalse((self.dir / "backup").exists())

    def test_version_mismatch_aborts(self):
        self.sys.download_version = "v1.19.34"
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "failed")
        self.assertIn("期望 v1.19.35", report["message"])
        self.assertEqual(self.core_text(), "VERSION=v1.19.30")

    def test_corrupt_gzip_aborts(self):
        self.sys.download_payload = b"\x1f\x8b\x08\x00garbage"
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "failed")
        self.assertIn("压缩包校验失败", report["message"])
        self.assertEqual(self.sys.restarts, 0)

    def test_dns_failure_rolls_back_and_rechecks(self):
        self.sys.dns_fail_versions = {"v1.19.35"}
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "rolled_back", report["message"])
        self.assertIn("回滚后检查通过", report["message"])
        self.assertEqual(self.core_text(), "VERSION=v1.19.30")
        self.assertEqual(self.sys.restarts, 2)
        self.assertEqual(self.sys.running_version, "v1.19.30")
        # 在 30 秒窗口内重试过，而不是一次失败就回滚
        self.assertGreaterEqual(self.clock[0] - NOW, self.au.HEALTH_TIMEOUT)

    def test_missing_listener_port_rolls_back(self):
        self.sys.missing_ports_versions = {"v1.19.35"}
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "rolled_back")
        self.assertIn("7895", report["message"])
        self.assertEqual(self.core_text(), "VERSION=v1.19.30")

    def test_controller_version_mismatch_rolls_back(self):
        rt = self.make_rt(controller_version=lambda: "v1.19.30")
        report = self.au.update_core(rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "rolled_back")
        self.assertIn("控制器报告版本", report["message"])

    def test_rollback_that_still_fails_says_manual_action_needed(self):
        self.sys.service_ok = False
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "rolled_back")
        self.assertIn("需要人工处理", report["message"])
        self.assertEqual(self.core_text(), "VERSION=v1.19.30")

    def test_backups_keep_last_three(self):
        backup_dir = self.dir / "backup"
        backup_dir.mkdir()
        for stamp in ("20260101000000", "20260201000000", "20260301000000", "20260401000000"):
            (backup_dir / f"mihomo-core.v1.19.1.{stamp}").write_text("old")
        self.au.update_core(self.rt, self.settings(), dry_run=False)
        backups = sorted(os.listdir(backup_dir))
        self.assertEqual(len(backups), 3)
        self.assertNotIn("mihomo-core.v1.19.1.20260101000000", backups)
        self.assertNotIn("mihomo-core.v1.19.1.20260201000000", backups)

    def test_dry_run_reports_without_changes(self):
        report = self.au.update_core(self.rt, self.settings(), dry_run=True)
        self.assertEqual(report["result"], "available")
        self.assertEqual(report["latest"], "v1.19.35")
        self.assertEqual(self.sys.downloads, [])
        self.assertEqual(self.sys.restarts, 0)

    def test_release_api_unavailable_skips(self):
        self.sys.fetch_ok = False
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "skipped")
        self.assertEqual(self.sys.downloads, [])

    def test_release_api_uses_direct_then_gh_proxy(self):
        (self.dir / ".env").write_text("GH_PROXY=https://gh-proxy.example/\n")
        self.au.update_core(self.rt, self.settings(), dry_run=True)
        urls = self.sys.fetched[0]
        self.assertEqual(urls[0], self.au.CORE_RELEASES_API)
        self.assertEqual(urls[1], "https://gh-proxy.example/" + self.au.CORE_RELEASES_API)

    def test_missing_core_binary_is_skipped(self):
        self.sys.core_bin.unlink()
        report = self.au.update_core(self.rt, self.settings(), dry_run=False)
        self.assertEqual(report["result"], "skipped")


class HealthTargetsTest(AutoUpdateTestBase):
    def test_yaml_and_fallback_parsers_agree(self):
        expected = {"dns": ("127.0.0.1", 1053), "ports": {7890, 7895, 7896}}
        if self.app.yaml is not None:
            self.assertEqual(self.au.read_health_targets(self.rt), expected)
        self.app.yaml = None
        self.assertEqual(self.au.read_health_targets(self.rt), expected)

    def test_dns_disabled_and_default_listen(self):
        (self.dir / "config.yaml").write_text("dns:\n  enable: false\n  listen: 0.0.0.0:1053\n")
        self.app.yaml = None
        self.assertIsNone(self.au.read_health_targets(self.rt)["dns"])
        (self.dir / "config.yaml").write_text("dns:\n  enable: true\n")
        self.assertEqual(self.au.read_health_targets(self.rt)["dns"], ("127.0.0.1", 53))

    def test_parse_listen_variants(self):
        self.assertEqual(self.au.parse_listen("[::]:53"), ("127.0.0.1", 53))
        self.assertEqual(self.au.parse_listen("127.0.0.1:1053"), ("127.0.0.1", 1053))
        self.assertEqual(self.au.parse_listen(":5353"), ("127.0.0.1", 5353))

    def test_dns_packet_roundtrip(self):
        query = self.au.build_dns_query("www.google.com", 0x1234)
        self.assertEqual(query[:2], b"\x12\x34")
        self.assertIn(b"\x03www\x06google\x03com\x00", query)
        answer = b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + query[12:]
        self.assertEqual(self.au.parse_dns_answer_count(answer, 0x1234)[0], 1)
        self.assertEqual(self.au.parse_dns_answer_count(answer, 0x9999)[0], 0)
        servfail = b"\x12\x34\x81\x82\x00\x01\x00\x00\x00\x00\x00\x00"
        self.assertEqual(self.au.parse_dns_answer_count(servfail, 0x1234), (0, "RCODE=2"))

    def test_proc_net_tcp_listen_parsing(self):
        text = (
            "  sl  local_address rem_address   st tx_queue rx_queue\n"
            "   0: 00000000:1ED2 00000000:0000 0A 00000000:00000000\n"
            "   1: 0100007F:2382 00000000:0000 0A 00000000:00000000\n"
            "   2: 0100007F:1ED3 0100007F:9C40 01 00000000:00000000\n"
        )
        self.assertEqual(self.au.parse_proc_net_tcp(text), {7890, 9090})


class DashboardUpdateTest(AutoUpdateTestBase):
    def setUp(self):
        super().setUp()
        self.ui = self.dir / "ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text("old dashboard")
        (self.ui / "assets").mkdir()
        (self.ui / "assets" / "app.js").write_text("old js")
        self.requests = []

    def controller(self, post_status=200, break_files=False, get_status=200):
        def request(method, path, timeout=60):
            self.requests.append((method, path))
            if method == "POST":
                if break_files:
                    (self.ui / "index.html").unlink()
                    (self.ui / "assets" / "app.js").write_text("half-written")
                else:
                    (self.ui / "index.html").write_text("new dashboard")
                return post_status, ""
            return get_status, ""
        return request

    def test_failure_restores_previous_files(self):
        rt = self.make_rt(controller_request=self.controller(break_files=True, get_status=404))
        report = self.au.update_ui(rt, self.settings(), self.state(), dry_run=False)
        self.assertEqual(report["result"], "rolled_back")
        self.assertEqual((self.ui / "index.html").read_text(), "old dashboard")
        self.assertEqual((self.ui / "assets" / "app.js").read_text(), "old js")
        self.assertEqual(self.sys.restarts, 0)
        self.assertFalse(any(name.startswith(".ui-backup.") for name in os.listdir(self.dir)))

    def test_upgrade_endpoint_error_restores(self):
        rt = self.make_rt(controller_request=self.controller(post_status=500))
        report = self.au.update_ui(rt, self.settings(), self.state(), dry_run=False)
        self.assertEqual(report["result"], "rolled_back")
        self.assertEqual((self.ui / "index.html").read_text(), "old dashboard")

    def test_success_records_last_success_and_interval_gate(self):
        rt = self.make_rt(controller_request=self.controller())
        report = self.au.run_updates(rt, only="ui")
        self.assertEqual(report["items"][0]["result"], "updated")
        self.assertEqual(self.requests, [("POST", "/upgrade/ui"), ("GET", "/ui/")])
        self.assertEqual(self.state()["items"]["ui"]["last_success"], NOW)
        # 6 天后：未到 7 天间隔，不发请求，也不覆盖上次结果
        self.clock[0] = NOW + 6 * DAY
        report = self.au.run_updates(rt, only="ui")
        self.assertEqual(report["items"][0]["result"], "not_due")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.state()["items"]["ui"]["last_result"], "updated")
        # 7 天（cron 时间略有漂移也算到期）
        self.clock[0] = NOW + 7 * DAY - 60
        report = self.au.run_updates(rt, only="ui")
        self.assertEqual(report["items"][0]["result"], "updated")

    def test_no_external_ui_skips(self):
        (self.dir / "config.yaml").write_text("mixed-port: 7890\n")
        report = self.au.update_ui(self.rt, self.settings(), self.state(), dry_run=False)
        self.assertEqual(report["result"], "skipped")


class PanelUpdateTest(AutoUpdateTestBase):
    def setUp(self):
        super().setUp()
        self.app.remote_panel_version = lambda settings=None: {"success": True, "latest_version": "9.9.9", "source": "x", "message": ""}

    def test_commits_api_unavailable_skips_never_fails_open(self):
        self.sys.commits = None
        report = self.au.run_panel_upgrade(self.rt, self.state(), self.settings(panel_min_age_days=2))
        self.assertEqual(report["result"], "skipped")
        self.assertIn("GitHub 提交记录不可用", report["message"])
        self.assertEqual(self.upgrade_calls, [])

    def test_too_new_commit_skips(self):
        self.sys.commits = [{"commit": {"committer": {"date": iso(NOW - DAY)}}}]
        report = self.au.run_panel_upgrade(self.rt, self.state(), self.settings(panel_min_age_days=2))
        self.assertEqual(report["result"], "skipped")
        self.assertIn("不足 2 天", report["message"])
        self.assertIn("path=remote-root", self.sys.fetched[-1][0])
        self.assertIn("sha=main", self.sys.fetched[-1][0])
        self.assertEqual(self.upgrade_calls, [])

    def test_old_enough_commit_upgrades_and_new_panel_finalizes(self):
        self.sys.commits = [{"commit": {"committer": {"date": iso(NOW - 3 * DAY)}}}]
        report = self.au.run_updates(self.rt, only="panel")
        self.assertEqual(self.upgrade_calls, [True])
        self.assertEqual(report["items"][0]["result"], "started")
        item = self.state()["items"]["panel"]
        self.assertEqual((item["last_result"], item["to"]), ("started", "9.9.9"))
        self.assertEqual(self.sys.notifications, [], "started 不发通知，等新面板确认")
        # 新面板启动：版本已到 → updated 并通知
        sent = []
        self.app.PANEL_VERSION = "9.9.9"
        result = self.app.finalize_panel_auto_update(self.rt.state_file, now=NOW + 30, notify=lambda t, c: sent.append(c))
        self.assertEqual(result, "updated")
        self.assertEqual(self.state()["items"]["panel"]["last_result"], "updated")
        self.assertIn("v9.9.9", sent[0])

    def test_pending_upgrade_that_never_arrives_becomes_failed(self):
        self.sys.commits = [{"commit": {"committer": {"date": iso(NOW - 3 * DAY)}}}]
        self.au.run_updates(self.rt, only="panel")
        self.assertIsNone(self.app.finalize_panel_auto_update(self.rt.state_file, now=NOW + 60, notify=None))
        self.assertEqual(self.app.finalize_panel_auto_update(self.rt.state_file, now=NOW + 3600, notify=None), "failed")

    def test_zero_min_age_does_not_need_commits_api(self):
        report = self.au.run_panel_upgrade(self.rt, self.state(), self.settings(panel_min_age_days=0))
        self.assertEqual(report["result"], "started")
        self.assertFalse(any("/commits?" in urls[0] for urls in self.sys.fetched))

    def test_not_newer_is_up_to_date(self):
        self.app.remote_panel_version = lambda settings=None: {"success": True, "latest_version": self.app.PANEL_VERSION}
        report = self.au.run_panel_upgrade(self.rt, self.state(), self.settings())
        self.assertEqual(report["result"], "up_to_date")
        self.assertEqual(self.upgrade_calls, [])

    def test_failed_upgrade_is_recorded_and_notified(self):
        self.upgrade_result = (False, "面板升级失败，已自动回滚：boom", False)
        report = self.au.run_updates(self.rt, only="panel")
        self.assertEqual(report["items"][0]["result"], "failed")
        self.assertEqual(len(self.sys.notifications), 1)


class RunOrchestrationTest(AutoUpdateTestBase):
    def setUp(self):
        super().setUp()
        self.sys.releases = [release("v1.19.35", 5)]
        self.app.remote_panel_version = lambda settings=None: {"success": True, "latest_version": "9.9.9"}
        (self.dir / "config.yaml").write_text(CONFIG.replace("external-ui: ui\n", ""))

    def test_core_rollback_postpones_panel_and_notifies(self):
        self.sys.dns_fail_versions = {"v1.19.35"}
        report = self.au.run_updates(self.rt)
        results = {item["item"]: item["result"] for item in report["items"]}
        self.assertEqual(results, {"ui": "skipped", "core": "rolled_back", "panel": "skipped"})
        self.assertFalse(report["success"])
        self.assertEqual(self.upgrade_calls, [])
        self.assertEqual(len(self.sys.notifications), 1)
        title, body = self.sys.notifications[0]
        self.assertEqual(title, "❌ mihomo 网关 · mihomo 内核更新失败，已回滚")
        self.assertEqual(body, "v1.19.30 → v1.19.35\n新版本没通过健康检查：DNS 无法解析\n已换回 v1.19.30，服务恢复正常")
        state = self.state()
        self.assertEqual(state["items"]["core"]["last_result"], "rolled_back")
        log_text = Path(self.app.AUTO_UPDATE_LOG).read_text()
        self.assertIn("[core]", log_text)

    def test_up_to_date_is_not_notified_and_order_is_ui_core_panel(self):
        self.sys.core_bin.write_text("VERSION=v1.19.35")
        self.app.remote_panel_version = lambda settings=None: {"success": True, "latest_version": self.app.PANEL_VERSION}
        report = self.au.run_updates(self.rt)
        self.assertEqual([item["item"] for item in report["items"]], ["ui", "core", "panel"])
        self.assertEqual(self.sys.notifications, [])
        self.assertTrue(report["success"])

    def test_dry_run_keeps_previous_results(self):
        state = self.state()
        state["items"]["core"] = {"last_result": "updated", "from": "v1.19.29", "to": "v1.19.30"}
        self.app.write_auto_update_state(state, self.rt.state_file)
        report = self.au.run_updates(self.rt, dry_run=True)
        self.assertEqual({i["item"]: i["result"] for i in report["items"]}["core"], "available")
        core = self.state()["items"]["core"]
        self.assertEqual(core["last_result"], "updated")
        self.assertEqual(core["latest"], "v1.19.35")
        self.assertEqual(core["last_dry_run"]["result"], "available")
        self.assertEqual(self.sys.restarts, 0)
        self.assertEqual(self.upgrade_calls, [])
        self.assertEqual(self.sys.notifications, [])

    def test_exception_in_one_item_is_recorded_as_failed(self):
        def boom():
            raise RuntimeError("controller exploded")
        self.app.remote_panel_version = lambda settings=None: boom()
        self.sys.core_bin.write_text("VERSION=v1.19.35")
        report = self.au.run_updates(self.rt)
        self.assertEqual(report["items"][-1]["result"], "failed")
        self.assertIn("controller exploded", report["items"][-1]["message"])


class LockTest(AutoUpdateTestBase):
    def test_lock_prevents_concurrent_runs(self):
        path = str(self.dir / "x.lock")
        first = self.app.acquire_auto_update_lock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(self.app.acquire_auto_update_lock(path))
        self.assertTrue(self.app.auto_update_lock_busy(path))
        self.app.release_auto_update_lock(first)
        self.assertFalse(self.app.auto_update_lock_busy(path))
        second = self.app.acquire_auto_update_lock(path)
        self.assertIsNotNone(second)
        self.app.release_auto_update_lock(second)

    def test_main_exits_when_another_run_holds_the_lock(self):
        lock_path = str(self.dir / "main.lock")
        self.app.auto_update_lock_path = lambda: lock_path
        self.au.load_panel = lambda: self.app
        holder = self.app.acquire_auto_update_lock(lock_path)
        try:
            ran = []
            self.au.run_updates = lambda *a, **k: ran.append(True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = self.au.main(["--manual", "--json"])
        finally:
            self.app.release_auto_update_lock(holder)
        self.assertEqual(code, self.au.EXIT_BUSY)
        self.assertEqual(ran, [])

    def test_main_respects_disabled_setting_unless_manual(self):
        (self.dir / ".env").write_text("AUTO_UPDATE_ENABLED=false\n")
        self.app.auto_update_lock_path = lambda: str(self.dir / "main.lock")
        self.au.load_panel = lambda: self.app
        ran = []
        self.au.run_updates = lambda *a, **k: ran.append(k) or {"success": True, "items": []}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.au.main(["--json"]), 0)
            self.assertEqual(ran, [])
            self.assertEqual(self.au.main(["--manual", "--json", "--dry-run", "--only", "core"]), 0)
        self.assertEqual(ran, [{"dry_run": True, "only": "core"}])


class FakeCrontab:
    def __init__(self, text=""):
        self.text = text
        self.writes = 0

    def run(self, args, capture_output=True, text=True, timeout=15, input=None):
        if args == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0 if self.text else 1, stdout=self.text, stderr="")
        if args == ["crontab", "-"]:
            self.text = input
            self.writes += 1
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)


class CronAndSettingsTest(AutoUpdateTestBase):
    def setUp(self):
        super().setUp()
        self.cron = FakeCrontab("0 5 * * * bash /etc/mihomo/scripts/update_subscription.sh # JOB_SUB\n")
        self.app.subprocess = types.SimpleNamespace(run=self.cron.run)

    def auto_lines(self):
        return [line for line in self.cron.text.splitlines() if "# MIHOMO_AUTO_UPDATE" in line]

    def test_cron_line_added_from_settings_and_kept_idempotent(self):
        ok, _ = self.app.ensure_auto_update_cron(self.settings(time="03:45"))
        self.assertTrue(ok)
        self.assertEqual(self.auto_lines(), [
            f"45 3 * * * python3 {self.app.MANAGER_DIR}/auto_update.py >/dev/null 2>>{self.app.AUTO_UPDATE_LOG} # MIHOMO_AUTO_UPDATE"
        ])
        self.assertIn("# JOB_SUB", self.cron.text)
        writes = self.cron.writes
        self.app.ensure_auto_update_cron(self.settings(time="03:45"))
        self.assertEqual(self.cron.writes, writes, "一致时不重写 crontab")
        self.app.ensure_auto_update_cron(self.settings(time="04:10"))
        self.assertEqual(len(self.auto_lines()), 1)
        self.assertTrue(self.auto_lines()[0].startswith("10 4 * * * "))

    def test_cron_line_removed_when_disabled(self):
        self.app.ensure_auto_update_cron(self.settings())
        self.assertEqual(len(self.auto_lines()), 1)
        self.app.ensure_auto_update_cron(self.settings(enabled=False))
        self.assertEqual(self.auto_lines(), [])
        self.assertIn("# JOB_SUB", self.cron.text)

    def test_defaults_from_empty_env(self):
        self.assertEqual(self.app.auto_update_settings({}), {
            "enabled": True, "time": "04:00", "core_min_age_days": 3, "panel_min_age_days": 0, "ui_interval_days": 7,
        })
        tolerant = self.app.auto_update_settings({"AUTO_UPDATE_TIME": "25:00", "AUTO_UPDATE_CORE_MIN_AGE_DAYS": "x",
                                                  "AUTO_UPDATE_PANEL_MIN_AGE_DAYS": "2", "AUTO_UPDATE_ENABLED": "false"})
        self.assertEqual((tolerant["time"], tolerant["core_min_age_days"], tolerant["panel_min_age_days"], tolerant["enabled"]),
                         ("04:00", 3, 2, False))

    def test_settings_validation(self):
        validate = self.app.validate_auto_update_settings
        good = {"enabled": "true", "time": "23:59", "core_min_age_days": "6", "panel_min_age_days": 2, "ui_interval_days": "7"}
        ok, _, normalized = validate(good)
        self.assertTrue(ok)
        self.assertEqual(normalized, {"enabled": True, "time": "23:59", "core_min_age_days": 6, "panel_min_age_days": 2, "ui_interval_days": 7})
        for key, bad in (("time", "4:00"), ("time", "24:00"), ("time", "04:60"), ("time", "04:00; rm -rf /"),
                         ("core_min_age_days", 91), ("core_min_age_days", -1), ("core_min_age_days", "1.5"),
                         ("panel_min_age_days", "abc"), ("ui_interval_days", 0), ("ui_interval_days", 366),
                         ("enabled", "yes")):
            ok, message, _ = validate({**good, key: bad})
            self.assertFalse(ok, f"{key}={bad!r} 应该被拒绝")
            self.assertTrue(message)

    def test_env_updates_round_trip(self):
        ok, _, normalized = self.app.validate_auto_update_settings(
            {"enabled": False, "time": "06:30", "core_min_age_days": 6, "panel_min_age_days": 2, "ui_interval_days": 14})
        self.app.write_env(self.app.auto_update_env_updates(normalized))
        self.assertEqual(self.app.auto_update_settings(), normalized)


if __name__ == "__main__":
    unittest.main()
