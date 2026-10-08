"""订阅自动更新链路：装完就有定时任务、每次运行留下结果、机场模式会触发节点刷新。"""
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INSTALL = ROOT / "install.sh"
SCRIPT = ROOT / "remote-root" / "etc" / "mihomo" / "scripts" / "update_subscription.sh"
INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"
LOGROTATE = ROOT / "remote-root" / "etc" / "logrotate.d" / "mihomo"


def load_app(tmp_dir):
    flask = types.ModuleType("flask")

    class FakeFlask:
        permanent_session_lifetime = None
        secret_key = None

        def __init__(self, *args, **kwargs):
            self.config = {}

        def route(self, *args, **kwargs):
            return lambda func: func

        def before_request(self, func):
            return func

    for name in ("render_template", "request", "jsonify", "Response", "redirect", "session"):
        setattr(flask, name, None)
    flask.Flask = FakeFlask
    sys.modules["flask"] = flask
    source = APP.read_text(encoding="utf-8").replace('MIHOMO_DIR = "/etc/mihomo"', f'MIHOMO_DIR = "{tmp_dir}"')
    module = types.ModuleType("mihomo_app_sub_test")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


class SubscriptionScheduleContractTest(unittest.TestCase):
    def test_installer_creates_cron_job_and_default_schedule(self):
        text = INSTALL.read_text(encoding="utf-8")
        self.assertIn('write_env_line "CRON_SUB_ENABLED" "true"', text)
        self.assertIn('write_env_line "CRON_SUB_SCHED" "0 5 * * *"', text)
        self.assertIn("install_cron_jobs() {", text)
        self.assertIn("update_subscription.sh >> /var/log/mihomo-subscription.log 2>&1 # JOB_SUB", text)
        self.assertIn("systemctl enable --now cron", text)
        main_body = text[text.find("main() {"):]
        self.assertLess(main_body.find("write_env_file"), main_body.find("install_cron_jobs"))

    def test_panel_cron_commands_log_instead_of_devnull(self):
        text = APP.read_text(encoding="utf-8")
        self.assertIn("update_subscription.sh >> {SUBSCRIPTION_LOG} 2>&1", text)
        self.assertIn("update_geo.sh >> {GEO_LOG} 2>&1", text)
        self.assertNotIn("update_subscription.sh >/dev/null 2>&1", text)
        self.assertIn('"last_subscription": last_subscription_state()', text)

    def test_script_records_state_sends_clash_ua_and_refreshes_providers(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('STATE_FILE="${MIHOMO_DIR}/.last_subscription"', text)
        self.assertIn("trap on_exit EXIT", text)
        self.assertIn('--user-agent="$SUB_USER_AGENT"', text)
        self.assertIn("/providers/proxies/${encoded}", text)
        self.assertIn('if [ "$CONFIG_MODE" != "raw" ]; then\n    refresh_proxy_providers || exit 1', text)

    def test_ui_and_logrotate_cover_the_new_state_and_logs(self):
        self.assertIn("formatLastSubscription(settings.last_subscription)", INDEX.read_text(encoding="utf-8"))
        rotate = LOGROTATE.read_text(encoding="utf-8")
        self.assertIn("/var/log/mihomo-subscription.log", rotate)
        self.assertIn("/var/log/mihomo-geo.log", rotate)


class LastSubscriptionStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_state_line(self):
        path = Path(self.tmp.name) / ".last_subscription"
        path.write_text("1760000000\tok\t配置无变更；已刷新 1 个订阅的节点\n", encoding="utf-8")
        self.assertEqual(self.app.last_subscription_state(str(path)), {"at": 1760000000, "ok": True, "message": "配置无变更；已刷新 1 个订阅的节点"})
        path.write_text("1760000001\tfailed\t托管配置下载失败\n", encoding="utf-8")
        self.assertFalse(self.app.last_subscription_state(str(path))["ok"])

    def test_missing_or_garbage_file_returns_none(self):
        self.assertIsNone(self.app.last_subscription_state(str(Path(self.tmp.name) / "nope")))
        path = Path(self.tmp.name) / ".last_subscription"
        path.write_text("garbage\n", encoding="utf-8")
        self.assertIsNone(self.app.last_subscription_state(str(path)))


class UpdateSubscriptionScriptTest(unittest.TestCase):
    """把脚本里的 /etc/mihomo 换成临时目录，用假的 systemctl / curl / wget 跑一遍机场模式。"""

    def run_script(self, env_lines, config_text, curl_code="204"):
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "templates").mkdir()
        (root / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        (root / "config.yaml").write_text(config_text, encoding="utf-8")
        (root / "templates" / "default.yaml").write_text(config_text, encoding="utf-8")
        (root / "scripts" / "notify.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        (fake_bin / "curl").write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$*\" >> '{root}/curl.log'\n"
            f"printf '{curl_code}'\n",
            encoding="utf-8",
        )
        # 机场模式下模板生成需要 PyYAML；没有就让脚本走 raw 模式之外的分支前提前失败，测试据此跳过
        for name in ("systemctl", "curl", "notify.sh"):
            path = fake_bin / name if name != "notify.sh" else root / "scripts" / name
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        script = SCRIPT.read_text(encoding="utf-8").replace('MIHOMO_DIR="/etc/mihomo"', f'MIHOMO_DIR="{root}"')
        script_path = root / "update_subscription.sh"
        script_path.write_text(script, encoding="utf-8")
        # 让脚本里的 python3 解析到当前解释器（跑测试的 venv 里才有 PyYAML）
        env = dict(os.environ, PATH=f"{fake_bin}:{os.path.dirname(sys.executable)}:{os.environ['PATH']}")
        result = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=env, timeout=60)
        return root, result

    def test_unconfigured_airport_mode_records_skip(self):
        root, result = self.run_script(['CONFIG_MODE="airport"', 'SUB_URL_AIRPORT=""'], "rules:\n  - MATCH,DIRECT\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = (root / ".last_subscription").read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual(state[1], "ok")
        self.assertIn("未配置机场订阅链接", state[2])

    def test_raw_mode_download_failure_records_failed(self):
        root, result = self.run_script(['CONFIG_MODE="raw"', 'SUB_URL_RAW="http://127.0.0.1:9/nope"'], "rules:\n  - MATCH,DIRECT\n")
        self.assertNotEqual(result.returncode, 0)
        state = (root / ".last_subscription").read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual(state[1], "failed")
        self.assertIn("下载失败", state[2])

    def test_provider_refresh_calls_controller_for_each_provider(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("机场模式生成配置需要 PyYAML")
        config = (
            "proxy-providers:\n  Airport1:\n    type: http\n    url: x\n    path: ./providers/a.yaml\n"
            "  \"机场 二\":\n    type: http\n    url: y\n    path: ./providers/b.yaml\n"
            "external-controller: 0.0.0.0:9090\nsecret: \"s3\"\nrules:\n  - MATCH,DIRECT\n"
        )
        root, result = self.run_script(['CONFIG_MODE="airport"', 'SUB_URL_AIRPORT="https://example.com/sub"'], config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = (root / "curl.log").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:9090/providers/proxies/Airport_01", calls)
        self.assertIn("Authorization: Bearer s3", calls)
        state = (root / ".last_subscription").read_text(encoding="utf-8")
        self.assertIn("\tok\t", state)
        self.assertIn("已刷新", state)


if __name__ == "__main__":
    unittest.main()
