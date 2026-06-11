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
        self.assertIn("def broadcast_rule", text)
        self.assertIn("def apply_synced_rules", text)
        self.assertIn('@app.route("/api/rule-sync-settings", methods=["GET", "POST"])', text)
        self.assertIn('@app.route("/api/rule-sync-test", methods=["POST"])', text)
        self.assertIn('@app.route("/api/rule-sync", methods=["POST"])', text)
        self.assertIn('@app.route("/api/rules/<rule_id>", methods=["GET", "POST"])', text)

    def test_mihomo_rules_are_written_before_normal_rules(self):
        text = app_source()

        self.assertIn("def build_mihomo_sync_rule_lines", text)
        self.assertIn("DOMAIN-SUFFIX,{domain},DIRECT", text)
        self.assertIn("DOMAIN-SUFFIX,{domain},{proxy_policy}", text)
        self.assertIn("def update_mihomo_sync_block", text)
        self.assertIn("rules_index = find_yaml_top_level_key", text)
        self.assertIn("lines[rules_index + 1:rules_index + 1] = block", text)
        self.assertIn("rules_index + 1", text)

    def test_sync_does_not_rebroadcast_received_rules(self):
        text = app_source()

        self.assertIn("def apply_synced_rules(rules):", text)
        self.assertNotIn("broadcast_rule(rule_id, content)", text[text.find("def apply_synced_rules"): text.find("def api_rule_sync")])

    def test_ui_has_force_rule_and_cross_panel_sync_controls(self):
        text = index_source()

        self.assertIn("force-cn", text)
        self.assertIn("force-nocn", text)
        self.assertIn("强制直连", text)
        self.assertIn("强制代理", text)
        self.assertIn("其他 mosctl / mihomo 面板地址", text)
        self.assertIn("loadSyncSettings", text)
        self.assertIn("saveSyncSettings", text)
        self.assertIn("testSyncPeers", text)

    def test_panel_version_rolls_forward_for_upgrade_detection(self):
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', app_source())

        self.assertIsNotNone(match)
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 1, 14))


if __name__ == "__main__":
    unittest.main()
