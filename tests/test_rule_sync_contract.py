from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


class MihomoRuleSyncContractTest(unittest.TestCase):
    def test_backend_exposes_mosctl_compatible_rule_sync(self):
        text = app_source()

        self.assertIn('SYNCABLE_RULE_IDS = {"force-cn", "force-nocn"}', text)
        self.assertIn('RULE_SYNC_ACTIONS = {"force-cn": "DIRECT", "force-nocn": "PROXY"}', text)
        self.assertIn("RULE_SYNC_BEGIN", text)
        self.assertIn("RULE_SYNC_END", text)
        self.assertIn("def read_sync_settings", text)
        self.assertIn("def write_sync_settings", text)
        self.assertIn("def start_broadcast", text)
        self.assertIn("def apply_synced_rules", text)
        self.assertIn('@app.route("/api/rule-sync-settings", methods=["GET", "POST"])', text)
        self.assertIn('@app.route("/api/rule-sync-test", methods=["POST"])', text)
        self.assertIn('@app.route("/api/rule-sync", methods=["POST"])', text)
        self.assertIn('@app.route("/api/rules/<rule_id>", methods=["GET", "POST"])', text)

    def test_mihomo_rules_are_written_before_normal_rules(self):
        text = app_source()

        self.assertIn("def build_mihomo_sync_rule_lines", text)
        self.assertIn("- RULE-SET,{RULE_PROVIDER_NAMES['force-cn']},DIRECT", text)
        self.assertIn("- RULE-SET,{RULE_PROVIDER_NAMES['force-nocn']},{proxy_policy}", text)
        self.assertIn("def update_mihomo_sync_block", text)
        self.assertIn("rules_index = find_yaml_top_level_key", text)
        self.assertIn("lines[rules_index + 1:rules_index + 1] = block", text)
        self.assertIn("rules_index + 1", text)

    def test_force_direct_rule_set_is_added_to_fake_ip_filter(self):
        text = app_source()

        self.assertIn("FAKE_IP_FILTER_BEGIN", text)
        self.assertIn("FAKE_IP_FILTER_END", text)
        self.assertIn("def build_fake_ip_filter_lines", text)
        section = text[text.find("def build_fake_ip_filter_lines"): text.find("def build_rule_provider_lines")]
        self.assertIn("- rule-set:{RULE_PROVIDER_NAMES['force-cn']}", section)
        self.assertNotIn("force-nocn", section)
        self.assertIn("update_fake_ip_filter_block(lines)", text)

    def test_rule_providers_are_file_based_and_hot_refreshed(self):
        text = app_source()

        self.assertIn('RULE_PROVIDER_NAMES = {"force-cn": "mosctl_force_cn", "force-nocn": "mosctl_force_nocn"}', text)
        self.assertIn('"force-cn": f"{RULES_DIR}/mosctl-force-cn.yaml"', text)
        self.assertIn("type: file, behavior: domain, format: yaml", text)
        self.assertIn('"/providers/rules/" + quote(name)', text)
        self.assertIn('method="PUT"', text)

    def test_sync_does_not_rebroadcast_received_rules(self):
        text = app_source()

        self.assertIn("def apply_synced_rules(rules):", text)
        self.assertNotIn("start_broadcast(", text[text.find("def apply_synced_rules"): text.find("def api_rule_sync")])

    def test_received_sync_defers_restart_until_after_http_response(self):
        text = app_source()
        sync_section = text[text.find("def apply_synced_rules"): text.find("def test_sync_peers")]

        self.assertIn("def schedule_mihomo_restart", text)
        self.assertIn("schedule_mihomo_restart()", sync_section)
        self.assertNotIn("restart_mihomo()", sync_section)

    def test_ui_has_force_rule_and_cross_panel_sync_controls(self):
        text = index_source()

        self.assertIn("force-cn", text)
        self.assertIn("force-nocn", text)
        self.assertIn("强制直连", text)
        self.assertIn("强制代理", text)
        self.assertIn("mosctl / mihomo 面板地址", text)
        self.assertIn("loadSyncSettings", text)
        self.assertIn("saveSyncSettings", text)
        self.assertIn("testSyncPeers", text)
        self.assertIn('id="syncLoadMode"', text)
        self.assertIn("'同步规则加载方式：' + res.load_mode", text)

    def test_config_editor_lets_backend_reload_or_restart(self):
        html = index_source()
        save_fn = html[html.find("async function saveConfig"): html.find("async function saveAndUpdateSub")]
        # 保存后由后端决定热加载/重启，前端不再另外调 control('restart')
        self.assertIn("api('/config', {content: content})", save_fn)
        self.assertNotIn("control('restart')", save_fn)
        self.assertIn("showToast(res.message", save_fn)
        app_text = app_source()
        self.assertIn('CONFIG_UNCHANGED_MESSAGE = "配置内容没有变化，未重启"', app_text)
        self.assertIn('"unchanged": True', app_text)

    def test_panel_version_rolls_forward_for_upgrade_detection(self):
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', app_source())

        self.assertIsNotNone(match)
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 1, 22))


if __name__ == "__main__":
    unittest.main()
