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

        self.assertIn('PANEL_VERSION = "0.1.7"', text)
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

    def test_panel_upgrade_messages_are_chinese(self):
        text = app_source()

        self.assertIn("Mihomo 面板升级完成", text)
        self.assertIn("Web 服务将在 1 秒后重启", text)
        self.assertIn("升级源", text)
        self.assertIn("仓库", text)
        self.assertIn("分支", text)
        self.assertIn("旧版本", text)
        self.assertIn("新版本", text)
        self.assertIn("备份位置", text)
        self.assertNotIn("Mihomo panel upgraded", text)
        self.assertNotIn("Web service will restart", text)
        self.assertNotIn("Old version:", text)
        self.assertNotIn("New version:", text)

    def test_panel_upgrade_prefers_github_proxy(self):
        text = app_source()

        self.assertIn('read_url_text([f"https://gh-proxy.com/{raw_url}", raw_url]', text)
        self.assertIn('download_file([f"https://gh-proxy.com/{archive_url}", archive_url]', text)
        self.assertNotIn('read_url_text([raw_url, f"https://gh-proxy.com/{raw_url}"]', text)
        self.assertNotIn('download_file([archive_url, f"https://gh-proxy.com/{archive_url}"]', text)

    def test_panel_upgrade_ui_contract_exists(self):
        text = index_source()

        self.assertIn("versionText", text)
        self.assertIn("versionLabel", text)
        self.assertIn("versionPopover", text)
        self.assertIn("versionPopoverCurrent", text)
        self.assertIn("versionPopoverState", text)
        self.assertIn("versionPopoverUpgrade", text)
        self.assertIn("loadPanelUpgradeSource", text)
        self.assertIn("confirmUpgradePanel", text)
        self.assertIn("handlePanelVersionClick", text)
        self.assertIn("renderVersionPopover", text)
        self.assertIn("toggleVersionPopover", text)
        self.assertIn("versionPopover.classList.toggle('show'", text)
        self.assertIn("document.addEventListener('click'", text)
        self.assertIn("version-dot", text)
        self.assertIn("upgrade_panel", text)
        self.assertIn("setTimeout(() => location.reload(), 5000)", text)

    def test_app_remains_valid_python(self):
        compile(app_source(), str(APP), "exec")

    def test_panel_version_is_semver(self):
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', app_source())

        self.assertIsNotNone(match)
        version = tuple(int(part) for part in match.groups())
        self.assertGreaterEqual(version, (0, 1, 7))


if __name__ == "__main__":
    unittest.main()
