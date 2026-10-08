"""改配置时能热加载就不重启：config_needs_restart / hot_reload_mihomo / apply_config_change，
以及配置编辑器、订阅更新脚本怎么接上它。"""
from pathlib import Path
import json
import tempfile
import types
import unittest
from urllib import error as urlerror
from urllib import request as real_urlrequest

from test_rule_sync_render import ROOT, FakeResponse, load_app

try:
    import yaml
except ImportError:  # pragma: no cover - 取决于环境
    yaml = None

INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"
SUB_SCRIPT = ROOT / "remote-root" / "etc" / "mihomo" / "scripts" / "update_subscription.sh"

BASE = """\
port: 7890
socks-port: 7891
mixed-port: 7893
redir-port: 7892
tproxy-port: 7894
allow-lan: true
bind-address: "*"
ipv6: false
log-level: info
external-controller: 0.0.0.0:9090
external-controller-tls: ""
external-ui: ui
secret: s3cret
interface-name: eth0
routing-mark: 6666
profile:
  store-selected: true
listeners:
  - {name: in1, type: mixed, port: 7895}
tun:
  enable: true
  stack: system
  auto-route: true
dns:
  enable: true
  listen: 0.0.0.0:1053
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  fake-ip-filter:
    - "*.lan"
  nameserver:
    - 223.5.5.5
proxies:
  - {name: a, type: socks5, server: 1.2.3.4, port: 1080}
proxy-groups:
  - {name: PROXY, type: select, proxies: [a]}
rule-providers:
  cn: {type: file, behavior: domain, format: yaml, path: ./rules/cn.yaml}
rules:
  - MATCH,PROXY
"""


def changed(key_path, value):
    data = yaml.safe_load(BASE)
    target = data
    keys = key_path.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


@unittest.skipUnless(yaml is not None, "需要 PyYAML")
class NeedsRestartTruthTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_network_structure_keys_need_restart(self):
        cases = {
            "tun": {"enable": False},
            "tun.stack": "gvisor",
            "port": 17890,
            "socks-port": 17891,
            "mixed-port": 17893,
            "redir-port": 17892,
            "tproxy-port": 17894,
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "ipv6": True,
            "listeners": [],
            "external-controller": "127.0.0.1:9091",
            "external-controller-tls": "0.0.0.0:9443",
            "external-ui": "ui2",
            "secret": "other",
            "interface-name": "eth1",
            "routing-mark": 255,
            "profile": {"store-selected": False},
            "dns.listen": "0.0.0.0:53",
            "dns.enable": False,
            "dns.enhanced-mode": "redir-host",
            "dns.fake-ip-range": "198.19.0.1/16",
        }
        for key_path, value in cases.items():
            with self.subTest(key=key_path):
                needs, reasons = self.app.config_needs_restart(BASE, changed(key_path, value))
                self.assertTrue(needs, key_path)
                top = key_path if key_path.startswith("dns.") else key_path.split(".")[0]
                self.assertEqual(reasons, [f"修改了 {top}"])

    def test_other_keys_hot_reload(self):
        cases = {
            "proxies": [{"name": "b", "type": "socks5", "server": "5.6.7.8", "port": 1080}],
            "rules": ["DOMAIN,example.com,DIRECT", "MATCH,PROXY"],
            "proxy-groups": [{"name": "PROXY", "type": "url-test", "proxies": ["a"]}],
            "rule-providers": {},
            "log-level": "debug",
            "dns.nameserver": ["119.29.29.29"],
            "dns.fake-ip-filter": ["*.lan", "+.local"],
        }
        for key_path, value in cases.items():
            with self.subTest(key=key_path):
                self.assertEqual(self.app.config_needs_restart(BASE, changed(key_path, value)), (False, []))
        # 只是格式/注释不同
        self.assertEqual(self.app.config_needs_restart(BASE, "# 注释\n" + BASE), (False, []))

    def test_unparseable_or_non_mapping_needs_restart(self):
        reason = ["无法比较配置，保守起见重启"]
        self.assertEqual(self.app.config_needs_restart(BASE, "port: [unclosed\n"), (True, reason))
        self.assertEqual(self.app.config_needs_restart("", BASE), (True, reason))
        self.assertEqual(self.app.config_needs_restart(BASE, "- just\n- a list\n"), (True, reason))


class NeedsRestartWithoutYamlTest(unittest.TestCase):
    def test_missing_pyyaml_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(tmp)
            app.yaml = None
            self.assertEqual(app.config_needs_restart(BASE, BASE + "# x\n"), (True, ["无法比较配置，保守起见重启"]))


class ApplyConfigChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        app = self.app = load_app(self.tmp.name)
        app.write_env({"MIHOMO_CONTROLLER": "127.0.0.1:9090", "MIHOMO_API_SECRET": "s3cret"})
        self.calls = []
        self.put_status = 204
        self.put_error = None
        self.version_error = None
        self.restarts = []
        self.restart_result = (True, "")

        def fake_urlopen(req, timeout=None):
            self.calls.append((req.get_method(), req.full_url, req.data, dict(req.header_items()), timeout))
            if req.get_method() == "PUT":
                if self.put_error is not None:
                    raise self.put_error
                return FakeResponse(self.put_status)
            if self.version_error is not None:
                raise self.version_error
            return FakeResponse(200)

        app.urlrequest = types.SimpleNamespace(Request=real_urlrequest.Request, urlopen=fake_urlopen)
        app.restart_mihomo = lambda: self.restarts.append(1) or self.restart_result
        self.needs = (False, [])
        app.config_needs_restart = lambda old, new: self.needs

    def tearDown(self):
        self.tmp.cleanup()

    def test_unchanged_does_nothing(self):
        result = self.app.apply_config_change(BASE, BASE.replace("\n", "\r\n"))
        self.assertEqual(result, (True, "配置内容没有变化，未重启", "unchanged"))
        self.assertEqual((self.calls, self.restarts), ([], []))

    def test_hot_reload_ok_does_not_restart(self):
        ok, message, action = self.app.apply_config_change(BASE, BASE + "# x\n")
        self.assertEqual((ok, message, action), (True, "已热加载，未中断连接", "reloaded"))
        self.assertEqual(self.restarts, [])
        method, url, data, headers, timeout = self.calls[0]
        self.assertEqual((method, url, timeout), ("PUT", "http://127.0.0.1:9090/configs?force=true", 30))
        self.assertEqual(json.loads(data), {"path": str(self.dir / "config.yaml"), "payload": ""})
        self.assertEqual(headers.get("Authorization"), "Bearer s3cret")
        # 热加载后确认控制器还在
        self.assertEqual([(c[0], c[1], c[4]) for c in self.calls[1:]], [("GET", "http://127.0.0.1:9090/version", 5)])

    def test_hot_reload_failure_falls_back_to_restart(self):
        for error in (urlerror.URLError("connection refused"), None):
            with self.subTest(error=error):
                self.calls.clear()
                self.restarts.clear()
                self.put_error = error
                self.put_status = 500 if error is None else 204
                ok, message, action = self.app.apply_config_change(BASE, BASE + "# x\n")
                self.assertTrue(ok)
                self.assertEqual(action, "restarted")
                self.assertTrue(message.startswith("热加载失败，已改为重启："), message)
                self.assertEqual(self.restarts, [1])

    def test_controller_silent_after_reload_falls_back_to_restart(self):
        self.version_error = urlerror.URLError("timed out")
        ok, message, action = self.app.apply_config_change(BASE, BASE + "# x\n")
        self.assertEqual((ok, action), (True, "restarted"))
        self.assertIn("热加载失败", message)
        self.assertEqual(self.restarts, [1])

    def test_network_change_restarts_without_put(self):
        self.needs = (True, ["修改了 tun", "修改了 dns.listen"])
        ok, message, action = self.app.apply_config_change(BASE, BASE + "# x\n")
        self.assertEqual((ok, message, action), (True, "已重启（因为修改了 tun，修改了 dns.listen）", "restarted"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.restarts, [1])

        self.restarts.clear()
        self.restart_result = (False, "Job failed")
        ok, message, action = self.app.apply_config_change(BASE, BASE + "# y\n")
        self.assertEqual((ok, action), (False, "restarted"))
        self.assertIn("Job failed", message)

    @unittest.skipUnless(yaml is not None, "需要 PyYAML")
    def test_real_comparison_end_to_end(self):
        app = load_app(self.tmp.name)
        app.urlrequest = self.app.urlrequest
        app.restart_mihomo = self.app.restart_mihomo
        ok, _message, action = app.apply_config_change(BASE, changed("log-level", "debug"))
        self.assertEqual((ok, action, self.restarts), (True, "reloaded", []))
        self.calls.clear()
        ok, message, action = app.apply_config_change(BASE, changed("tun.enable", False))
        self.assertEqual((ok, action, self.calls, self.restarts), (True, "restarted", [], [1]))
        self.assertIn("tun", message)


class ConfigEditorApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        app = self.app = load_app(self.tmp.name)
        app.session = {"logged_in": True}
        app.jsonify = lambda payload: payload
        app.validate_config = lambda path: (True, "ok")
        (self.dir / "config.yaml").write_text(BASE, encoding="utf-8")
        self.applied = []
        self.result = (True, "已热加载，未中断连接", "reloaded")
        app.apply_config_change = lambda old, new: self.applied.append((old, new)) or self.result

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, content):
        self.app.request = types.SimpleNamespace(
            method="POST", path="/api/config", headers={"X-Requested-With": "XMLHttpRequest"},
            get_json=lambda silent=True: {"content": content},
        )
        return self.app.handle_config()

    def test_save_applies_change_and_returns_action(self):
        new = BASE + "# edited\n"
        self.assertEqual(self.post(new), {"success": True, "message": "已热加载，未中断连接", "action": "reloaded"})
        self.assertEqual(self.applied, [(BASE, new)])
        self.assertEqual((self.dir / "config.yaml").read_text(encoding="utf-8"), new)

        self.result = (False, "mihomo 重启失败（修改了 tun）：\nboom", "restarted")
        result = self.post(BASE + "# again\n")
        self.assertEqual((result["success"], result["action"]), (False, "restarted"))
        self.assertTrue(result["message"].startswith("配置已保存，但"))

    def test_unchanged_save_does_not_apply(self):
        self.assertTrue(self.post(BASE)["unchanged"])
        self.assertEqual(self.applied, [])


class WiringContractTest(unittest.TestCase):
    def test_frontend_no_longer_restarts_after_config_save(self):
        html = INDEX.read_text(encoding="utf-8")
        save_fn = html[html.find("async function saveConfig"): html.find("async function saveAndUpdateSub")]
        self.assertNotIn("control('restart')", save_fn)
        self.assertNotIn("保存重启", html)

    def test_subscription_script_uses_helper_and_keeps_rollback(self):
        text = SUB_SCRIPT.read_text(encoding="utf-8")
        apply_at = text.find("app.apply_config_change(old_text, new_text)")
        self.assertGreater(apply_at, 0)
        self.assertIn('python3 - "$OLD_COPY" "$CONFIG_FILE"', text)
        self.assertIn('sys.path.insert(0, "/etc/mihomo/manager")', text)
        self.assertLess(text.find('cp "$CONFIG_FILE" "$OLD_COPY"'), text.find('mv "$TEMP_NEW" "$CONFIG_FILE"'))
        # 应用失败 / 没在运行 → 恢复备份并重启
        self.assertGreater(text.find("systemctl is-active --quiet mihomo || APPLY_FAILED=1"), apply_at)
        self.assertIn('cp "$BACKUP_FILE" "$CONFIG_FILE"\n            systemctl restart mihomo', text)
        self.assertIn("热加载", text[text.find('RESULT_MSG="配置已更新并热加载'):][:40])
        self.assertIn('RESULT_MSG="配置已更新并重启"', text)

    def test_manual_restart_actions_stay_real_restarts(self):
        app_text = (ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py").read_text(encoding="utf-8")
        control = app_text[app_text.find("def control_service"):]
        control = control[:control.find("\n@app.route")]
        # 用户手动点的重启 / 修复日志仍是真重启
        self.assertIn("'restart': ['systemctl', 'restart', 'mihomo'],", control)
        self.assertIn("'fix_logs': ['systemctl', 'restart', 'mihomo'],", control)
        self.assertNotIn("hot_reload_mihomo", control)


if __name__ == "__main__":
    unittest.main()
