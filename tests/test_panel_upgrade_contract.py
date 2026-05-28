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


class MihomoPanelUpgradeContractTest(unittest.TestCase):
    def test_panel_upgrade_backend_contract_exists(self):
        text = app_source()

        self.assertIn('PANEL_VERSION = "0.1.0"', text)
        self.assertIn("DEFAULT_PANEL_REPO_URL", text)
        self.assertIn("def panel_version_tuple", text)
        self.assertIn("def panel_upgrade_state", text)
        self.assertIn("def upgrade_panel", text)
        self.assertIn("@app.route('/api/panel-upgrade-source')", text)
        self.assertIn("action == 'upgrade_panel'", text)

    def test_panel_upgrade_preserves_local_state(self):
        text = app_source()

        self.assertIn("PANEL_UPGRADE_EXCLUDES", text)
        self.assertIn("/etc/mihomo/.env", text)
        self.assertIn("/etc/mihomo/config.yaml", text)
        self.assertIn("/etc/mihomo/ui", text)
        self.assertIn("remote_tuple <= current_tuple", text)

    def test_panel_upgrade_prefers_github_proxy(self):
        text = app_source()

        self.assertIn('read_url_text([f"https://gh-proxy.com/{raw_url}", raw_url]', text)
        self.assertIn('download_file([f"https://gh-proxy.com/{archive_url}", archive_url]', text)

    def test_panel_upgrade_ui_contract_exists(self):
        text = index_source()

        self.assertIn("panelRepo", text)
        self.assertIn("panelBranch", text)
        self.assertIn("panelCurrent", text)
        self.assertIn("panelLatest", text)
        self.assertIn("loadPanelUpgradeSource", text)
        self.assertIn("confirmUpgradePanel", text)
        self.assertIn("upgrade_panel", text)
        self.assertIn("setTimeout(() => location.reload(), 5000)", text)

    def test_app_remains_valid_python(self):
        compile(app_source(), str(APP), "exec")

    def test_panel_version_is_semver(self):
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', app_source())

        self.assertIsNotNone(match)


if __name__ == "__main__":
    unittest.main()
