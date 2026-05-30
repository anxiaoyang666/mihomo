from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"


def text(path):
    return path.read_text(encoding="utf-8")


class MihomoUiPolishContractTest(unittest.TestCase):
    def test_mobile_viewport_keeps_user_zoom_available(self):
        source = text(INDEX)

        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1.0">', source)
        self.assertNotIn("user-scalable=no", source)
        self.assertNotIn("maximum-scale", source)

    def test_feedback_uses_in_page_toasts_and_confirm_dialog(self):
        source = text(INDEX)

        self.assertIn('id="toastStack"', source)
        self.assertIn("function showToast", source)
        self.assertIn('id="confirmDialog"', source)
        self.assertIn("function askConfirm", source)
        self.assertNotIn("alert(", source)
        self.assertNotIn("confirm(", source)

    def test_action_buttons_have_touch_and_busy_states(self):
        source = text(INDEX)

        self.assertRegex(source, r"\.icon-button\s*\{[^}]*width:\s*44px")
        self.assertRegex(source, r"\.icon-button\s*\{[^}]*height:\s*44px")
        self.assertRegex(source, r"\.(?:soft-button,\s*\n\s*)?\.action-button\s*\{[^}]*min-height:\s*44px")
        self.assertIn("function setActionBusy", source)
        self.assertIn("classList.toggle('is-busy'", source)
        self.assertIn("button-spinner", source)

    def test_logs_can_be_filtered_and_cleared_in_browser(self):
        source = text(INDEX)

        self.assertIn('id="logSearch"', source)
        self.assertIn('id="logLevelFilter"', source)
        self.assertIn('id="logPauseSwitch"', source)
        self.assertIn("function renderLogs", source)
        self.assertIn("function applyLogFilters", source)
        self.assertIn("function clearLogDisplay", source)

    def test_icon_only_controls_have_accessible_names(self):
        source = text(INDEX)

        self.assertIn('aria-label="切换主题"', source)
        self.assertIn('aria-label="启动 Mihomo"', source)
        self.assertIn('aria-label="重启 Mihomo"', source)
        self.assertIn('aria-label="停止 Mihomo"', source)

    def test_release_version_is_0112(self):
        app = text(APP)

        self.assertIn('PANEL_VERSION = "0.1.12"', app)
        match = re.search(r'(?m)^PANEL_VERSION = "(\d+)\.(\d+)\.(\d+)"$', app)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 1, 12))


if __name__ == "__main__":
    unittest.main()
