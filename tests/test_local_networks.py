"""本地直连网段（LOCAL_CIDR）：规范化、自动识别、订阅脚本注入、/api/settings 不因缺键清空 .env、页面契约。"""
from pathlib import Path
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

from test_rule_sync_render import load_app

try:
    import yaml
except ImportError:  # pragma: no cover - 取决于环境
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "remote-root" / "etc" / "mihomo"
APP = MIHOMO / "manager" / "app.py"
INDEX = MIHOMO / "manager" / "templates" / "index.html"
SCRIPT = MIHOMO / "scripts" / "update_subscription.sh"

ACCEPTED = [
    ("10.10.20.0/24", ["10.10.20.0/24"]),
    ("10.10.20.5/24", ["10.10.20.0/24"]),          # 主机位清零
    ("10.10.20.0", ["10.10.20.0/24"]),             # 不带掩码 → /24
    ("10.10.20.5", ["10.10.20.0/24"]),
    ("10.10.20", ["10.10.20.0/24"]),               # 三段 → .0/24
    ("192.168.1.0/255.255.255.0", ["192.168.1.0/24"]),
    ("172.20.3.4/16", ["172.20.0.0/16"]),
    ("100.64.1.1", ["100.64.1.0/24"]),             # CGNAT
    ("10.0.0.0/8", ["10.0.0.0/8"]),
    ("10.10.20.7/32", ["10.10.20.7/32"]),
    ("fd00:1::/48", ["fd00:1::/48"]),
    ("fd00:1:2:3::99", ["fd00:1:2:3::/64"]),       # IPv6 不带前缀 → /64
    ("fd12:3456::1/56", ["fd12:3456::/56"]),
    ("10.10.20.0/24, 192.168.1.0/24", ["10.10.20.0/24", "192.168.1.0/24"]),
    ("10.10.20.0/24，192.168.1.0/24", ["10.10.20.0/24", "192.168.1.0/24"]),
    ("10.10.20.0/24\n192.168.1.0 \t 10.10.30", ["10.10.20.0/24", "192.168.1.0/24", "10.10.30.0/24"]),
    ("10.10.20.5, 10.10.20.0/24, 10.10.20", ["10.10.20.0/24"]),   # 去重保序
    ("192.168.1.0/24,10.10.20.0/24", ["192.168.1.0/24", "10.10.20.0/24"]),
    ("", []),
    ("  ,， \n", []),
    (None, []),
    (",".join(f"10.10.{i}.0/24" for i in range(8)), [f"10.10.{i}.0/24" for i in range(8)]),
]

REJECTED = [
    ("8.8.8.0/24", "只能填写局域网网段"),
    ("1.1.1.1", "只能填写局域网网段"),
    ("198.18.0.0/16", "只能填写局域网网段"),     # TUN 地址段
    ("127.0.0.0/8", "只能填写局域网网段"),
    ("172.0.0.0/8", "只能填写局域网网段"),
    ("2001:db8::/64", "只能填写局域网网段"),
    ("0.0.0.0/0", "范围太大"),
    ("10.0.0.0/7", "范围太大"),
    ("fd00::/16", "范围太大"),
    ("::/0", "范围太大"),
    ("abc", "「abc」"),
    ("10.10.20.0/24, 10.10.300.0", "「10.10.300.0」"),
    ("10.10.20.0/33", "「10.10.20.0/33」"),
    ("10.10", "「10.10」"),
    (",".join(f"10.10.{i}.0/24" for i in range(9)), "最多填写 8 个网段"),
]

IP_OUTPUT = textwrap.dedent("""\
    2: eth0    inet 10.10.20.5/24 brd 10.10.20.255 scope global eth0\\       valid_lft forever preferred_lft forever
    3: eth1    inet 192.168.8.2/23 brd 192.168.9.255 scope global dynamic eth1\\       valid_lft 86000sec preferred_lft 86000sec
    4: eth0.10@eth0    inet 10.10.20.9/24 scope global secondary eth0.10\\       valid_lft forever preferred_lft forever
    5: Meta    inet 198.18.0.1/30 brd 198.18.0.3 scope global Meta\\       valid_lft forever preferred_lft forever
    6: tun0    inet 172.19.0.1/30 scope global tun0\\       valid_lft forever preferred_lft forever
    7: br0    inet 198.19.0.1/16 scope global br0\\       valid_lft forever preferred_lft forever
    8: wan0    inet 203.0.113.7/24 scope global wan0\\       valid_lft forever preferred_lft forever
    garbage line
""")


def text(path):
    return Path(path).read_text(encoding="utf-8")


def script_stage():
    """订阅脚本里防回环阶段的 Python 源码。"""
    source = text(SCRIPT)
    start = source.index('python3 - "$TEMP_NEW" "${MIHOMO_DIR}/manager" <<\'PY\'\n')
    body = source[source.index("\n", start) + 1:]
    return body[:body.index("\nPY\n")]


def fallback_module():
    stage = script_stage()
    block = stage[stage.index("# BEGIN_LOCAL_NETWORKS_FALLBACK"):stage.index("# END_LOCAL_NETWORKS_FALLBACK")]
    module = types.ModuleType("local_networks_fallback")
    exec("import ipaddress, re, subprocess\n" + block, module.__dict__)
    return module


class NormalizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = load_app(cls.tmp.name)
        cls.fallback = fallback_module()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_accepted(self):
        for impl in (self.app, self.fallback):
            for raw, expected in ACCEPTED:
                with self.subTest(impl=impl.__name__, raw=raw):
                    self.assertEqual(impl.normalize_local_networks(raw), (expected, None))

    def test_rejected(self):
        for impl in (self.app, self.fallback):
            for raw, fragment in REJECTED:
                with self.subTest(impl=impl.__name__, raw=raw):
                    networks, error = impl.normalize_local_networks(raw)
                    self.assertEqual(networks, [])
                    self.assertIn(fragment, error or "")

    def test_list_input(self):
        self.assertEqual(self.app.normalize_local_networks(["10.10.20.1", "10.10.21"]), (["10.10.20.0/24", "10.10.21.0/24"], None))

    def test_parse_ip_output_skips_tun_and_public(self):
        for impl in (self.app, self.fallback):
            with self.subTest(impl=impl.__name__):
                self.assertEqual(impl.parse_ip_addr_networks(IP_OUTPUT), ["10.10.20.0/24", "192.168.8.0/23"])
                self.assertEqual(impl.parse_ip_addr_networks(""), [])

    def test_effective_networks(self):
        detect = lambda: ["10.10.20.0/24"]
        for impl in (self.app, self.fallback):
            with self.subTest(impl=impl.__name__):
                self.assertEqual(impl.effective_local_networks("192.168.1.5", detect), (["192.168.1.0/24"], False, None))
                self.assertEqual(impl.effective_local_networks("", detect), (["10.10.20.0/24"], True, None))
                nets, auto, error = impl.effective_local_networks("8.8.8.8", detect)
                self.assertEqual((nets, auto), (["10.10.20.0/24"], True))
                self.assertIn("局域网", error)

    def test_detect_runs_ip_command(self):
        calls = []
        real_run = self.app.subprocess.run

        def fake_run(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout=IP_OUTPUT, stderr="")
        self.app.subprocess.run = fake_run
        try:
            self.assertEqual(self.app.detect_local_networks(use_cache=False), ["10.10.20.0/24", "192.168.8.0/23"])
        finally:
            self.app.subprocess.run = real_run
        self.assertEqual(calls, [["ip", "-4", "-o", "addr", "show", "scope", "global"]])


@unittest.skipIf(yaml is None, "需要 PyYAML")
class SubscriptionStageTest(unittest.TestCase):
    def run_stage(self, local_cidr, config_text, ip_output=""):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        config = tmp / "config.yaml"
        config.write_text(config_text, encoding="utf-8")
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        (tmp / "ip.txt").write_text(ip_output, encoding="utf-8")
        ip = fake_bin / "ip"
        ip.write_text(f"#!/bin/bash\ncat '{tmp}/ip.txt'\n", encoding="utf-8")
        ip.chmod(ip.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, LOCAL_CIDR=local_cidr, PATH=f"{fake_bin}:{os.environ['PATH']}")
        result = subprocess.run([sys.executable, "-", str(config), str(tmp / "no-manager")], input=script_stage(),
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return yaml.safe_load(config.read_text(encoding="utf-8")), result.stdout

    def test_multiple_networks_and_ipv6(self):
        config, out = self.run_stage(
            "10.10.20.5, 192.168.1.0/24 fd00:1::/48",
            "tun:\n  route-exclude-address: [192.168.1.0/24, 172.30.0.0/16]\n"
            "rules:\n  - IP-CIDR,192.168.1.0/24,DIRECT,no-resolve\n  - IP-CIDR,10.10.30.0/24,Home-02\n  - MATCH,PROXY\n")
        self.assertEqual(config["rules"], [
            "IP-CIDR,10.10.20.0/24,DIRECT,no-resolve",
            "IP-CIDR,192.168.1.0/24,DIRECT,no-resolve",
            "IP-CIDR6,fd00:1::/48,DIRECT,no-resolve",
            "IP-CIDR,10.10.30.0/24,Home-02",
            "MATCH,PROXY",
        ])
        self.assertEqual(config["tun"]["route-exclude-address"],
                         ["10.10.20.0/24", "192.168.1.0/24", "fd00:1::/48", "172.30.0.0/16"])
        self.assertIn("已插入规则", out)

    def test_empty_uses_detected_networks(self):
        config, out = self.run_stage("", "rules:\n  - MATCH,PROXY\n", IP_OUTPUT)
        self.assertEqual(config["rules"][:2], ["IP-CIDR,10.10.20.0/24,DIRECT,no-resolve", "IP-CIDR,192.168.8.0/23,DIRECT,no-resolve"])
        self.assertEqual(config["tun"]["route-exclude-address"], ["10.10.20.0/24", "192.168.8.0/23"])
        self.assertIn("自动识别", out)

    def test_nothing_detected_leaves_config(self):
        config, out = self.run_stage("", "rules:\n  - MATCH,PROXY\n", "")
        self.assertEqual(config, {"rules": ["MATCH,PROXY"]})
        self.assertIn("跳过防回环注入", out)


class SettingsPostTest(unittest.TestCase):
    ENV = textwrap.dedent("""\
        CONFIG_MODE=raw
        SUB_URL_RAW=https://example.com/raw
        SUB_URL_AIRPORT='https://a.example/1\\nhttps://a.example/2'
        NOTIFY_API=true
        NOTIFY_API_URL=https://hook.example/x
        LOCAL_CIDR=10.10.20.0/24
        CRON_SUB_ENABLED=true
        CRON_SUB_SCHED='30 3 * * *'
        CRON_SUB_MODE=daily
        CRON_SUB_TIME=03:30
        CRON_GEO_ENABLED=true
        CRON_GEO_SCHED='0 4 * * *'
        CRON_GEO_MODE=daily
        CRON_GEO_TIME=04:00
        SITE_NAME=联通
    """)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        Path(self.tmp.name, ".env").write_text(self.ENV, encoding="utf-8")
        self.crons = []
        self.app.update_cron = lambda *args: self.crons.append(args) or (True, "")
        self.app.ensure_auto_update_cron = lambda *a, **k: (True, "")
        self.app.jsonify = lambda payload: payload
        self.app.detect_local_networks = lambda *a, **k: ["192.168.8.0/24"]

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, body=None):
        self.app.request = types.SimpleNamespace(method=method, get_json=lambda silent=False: body)
        return self.app.handle_settings.__wrapped__()

    def test_missing_keys_leave_env_untouched(self):
        before = self.app.read_env()
        res = self.call("POST", {})
        self.assertTrue(res["success"])
        self.assertEqual(self.app.read_env(), before)
        self.assertEqual(self.crons, [])

    def test_only_local_cidr_changes(self):
        before = self.app.read_env()
        res = self.call("POST", {"local_cidr": "10.10.21.7，192.168.1"})
        self.assertTrue(res["success"])
        after = self.app.read_env()
        self.assertEqual(after.pop("LOCAL_CIDR"), "10.10.21.0/24,192.168.1.0/24")
        before.pop("LOCAL_CIDR")
        self.assertEqual(after, before)

    def test_invalid_local_cidr_rejected(self):
        before = self.app.read_env()
        res, status = self.call("POST", {"local_cidr": "8.8.8.0/24", "sub_url_raw": "https://other"})
        self.assertEqual(status, 400)
        self.assertFalse(res["success"])
        self.assertIn("只能填写局域网网段", res["message"])
        self.assertEqual(self.app.read_env(), before)

    def test_empty_local_cidr_means_auto(self):
        self.call("POST", {"local_cidr": ""})
        self.assertEqual(self.app.read_env()["LOCAL_CIDR"], "")
        res = self.call("GET")
        self.assertEqual(res["local_networks_effective"], ["192.168.8.0/24"])
        self.assertTrue(res["local_networks_auto"])

    def test_get_reports_explicit_networks(self):
        res = self.call("GET")
        self.assertEqual(res["local_cidr"], "10.10.20.0/24")
        self.assertEqual(res["local_networks_effective"], ["10.10.20.0/24"])
        self.assertFalse(res["local_networks_auto"])

    def test_partial_cron_merges_with_env(self):
        self.call("POST", {"cron_sub_enabled": "false"})
        env = self.app.read_env()
        self.assertEqual((env["CRON_SUB_ENABLED"], env["CRON_SUB_SCHED"], env["CRON_SUB_TIME"]), ("false", "30 3 * * *", "03:30"))
        self.assertEqual(env["CRON_GEO_ENABLED"], "true")
        self.assertEqual(len(self.crons), 1)
        self.assertEqual(self.crons[0][0], "# JOB_SUB")
        self.assertFalse(self.crons[0][3])

    def test_full_payload_still_saves_everything(self):
        res = self.call("POST", {
            "config_mode": "airport", "sub_url_raw": "", "sub_url_airport": "https://b/1|https://b/2",
            "notify_api": "false", "api_url": "", "local_cidr": "10.10.20.0/24",
            "cron_sub_enabled": "true", "cron_sub_mode": "every6h", "cron_sub_time": "00:15", "cron_sub_sched": "",
            "cron_geo_enabled": "false", "cron_geo_mode": "daily", "cron_geo_time": "04:00", "cron_geo_sched": "",
        })
        self.assertTrue(res["success"])
        env = self.app.read_env()
        self.assertEqual(env["CONFIG_MODE"], "airport")
        self.assertEqual(env["SUB_URL_RAW"], "")
        self.assertEqual(env["NOTIFY_API"], "false")
        self.assertEqual(env["CRON_SUB_SCHED"], "15 */6 * * *")
        self.assertEqual(env["CRON_GEO_ENABLED"], "false")
        self.assertEqual(env["SITE_NAME"], "联通")
        self.assertEqual([c[0] for c in self.crons], ["# JOB_SUB", "# JOB_GEO"])


class LocalNetworksUiContractTest(unittest.TestCase):
    def setUp(self):
        self.html = text(INDEX)

    def test_label_help_placeholder_preview(self):
        self.assertIn(">本地直连网段</label>", self.html)
        self.assertIn('placeholder="留空自动识别，例如 10.10.20.0 或 10.10.20.0/24，多个用逗号分隔"', self.html)
        self.assertIn('id="local_cidr_help"', self.html)
        help_text = re.search(r'id="local_cidr_help">([^<]+)<', self.html).group(1)
        self.assertIn("隧道", help_text)
        self.assertIn("留空会自动使用这台机器所在的网段", help_text)
        self.assertIn('id="local_cidr_preview"', self.html)
        self.assertIn('oninput="updateLocalCidrPreview()"', self.html)
        self.assertIn("'将使用：' + networks.join(', ')", self.html)
        self.assertIn("将使用：自动识别（当前 ${current}）", self.html)
        self.assertNotIn("留空则不启用", self.html)

    def test_overview_pill_shows_effective_and_auto(self):
        self.assertIn("['直连网段', formatLocalNetworks(settings.local_networks_effective, settings.local_networks_auto)", self.html)
        self.assertIn("(auto ? '（自动）' : '')", self.html)

    def test_save_validates_locally_and_aborts(self):
        save = self.html[self.html.index("async function saveSettings("):]
        save = save[:save.index("const res = await api('/settings', payload);")]
        self.assertIn("normalizeLocalNetworks(", save)
        self.assertIn("showToast('本地直连网段：' + localCheck.error, 'error');", save)

    def test_save_buttons_disabled_until_loaded(self):
        buttons = re.findall(r'<button[^>]*onclick="(?:saveSettings|saveAndUpdateSub)\([^"]*"[^>]*>', self.html)
        self.assertEqual(len(buttons), 3)
        for button in buttons:
            self.assertIn("data-requires-settings", button)
            self.assertRegex(button, r"\sdisabled\s")
        load = self.html[self.html.index("    function loadSettings() {"):]
        load = load[:load.index("\n    }\n")]
        self.assertIn("res.success === false", load)
        self.assertIn("showToast('设置未加载，暂时不能保存', 'error');", load)
        self.assertLess(load.index("updateScheduleVisibility('geo'"), load.index("settingsLoaded = true;"))
        self.assertLess(load.index("settingsLoaded = true;"), load.index("setSettingsButtonsEnabled(true);"))
        self.assertIn("button.disabled = !enabled;", self.html)
        for fn in ("async function saveSettings(", "async function saveAndUpdateSub("):
            body = self.html[self.html.index(fn):]
            self.assertIn("if (!settingsLoaded)", body[:body.index("setActionBusy(trigger, true);")])

    @unittest.skipIf(shutil.which("node") is None, "需要 node")
    def test_client_normalization_matches_server_for_ipv4(self):
        html = self.html
        start = html.index("    const LOCAL_NETWORKS_MAX = 8;")
        end = html.index("    function formatLocalNetworks(")
        cases = [raw for raw, _ in ACCEPTED if raw is not None and ":" not in raw and "255.255" not in raw]
        cases += [raw for raw, _ in REJECTED if ":" not in raw]
        js = html[start:end] + "\nconsole.log(JSON.stringify(%s.map(t => normalizeLocalNetworks(t))));" % __import__("json").dumps(cases)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = __import__("json").loads(out.stdout)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = load_app(tmp.name)
        for raw, client in zip(cases, results):
            with self.subTest(raw=raw):
                networks, error = app.normalize_local_networks(raw)
                self.assertEqual(client["networks"], networks)
                self.assertEqual(client["error"], error)


if __name__ == "__main__":
    unittest.main()
