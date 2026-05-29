from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
README = ROOT / "README.md"


def install_source():
    return INSTALL.read_text(encoding="utf-8")


def readme_source():
    return README.read_text(encoding="utf-8")


class MihomoInstallContractTest(unittest.TestCase):
    def test_one_click_installer_exists(self):
        self.assertTrue(INSTALL.exists())

        text = install_source()
        self.assertIn('PROJECT_NAME="Mihomo Toolbox"', text)
        self.assertIn('REPO_URL="${MIHOMO_REPO_URL:-https://github.com/anxiaoyang666/mihomo.git}"', text)
        self.assertIn('WEB_PORT="${WEB_PORT:-7838}"', text)
        self.assertIn('install -m 0755 "$payload/usr/bin/mihomo" /usr/bin/mihomo', text)
        self.assertIn('install -m 0644 "$payload/etc/systemd/system/mihomo.service"', text)
        self.assertIn('WEB_SESSION_SECRET="$session_secret"', text)
        self.assertIn('MIHOMO_PANEL_REPO_URL="$REPO_URL"', text)
        self.assertIn('systemctl restart mihomo-manager', text)
        self.assertIn('cp "$payload/etc/mihomo/config.example.yaml" "$INSTALL_DIR/config.yaml"', text)
        self.assertNotIn('cp "$payload/etc/mihomo/config.yaml" "$INSTALL_DIR/config.yaml"', text)

    def test_readme_documents_one_click_install(self):
        text = readme_source()

        self.assertIn("## One-Click Install", text)
        self.assertIn(
            'bash -c "$(curl -fsSL https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"',
            text,
        )
        self.assertIn(
            'bash -c "$(curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/anxiaoyang666/mihomo/main/install.sh)"',
            text,
        )
        self.assertIn("WEB_PORT=7838 WEB_USER=admin WEB_SECRET='your-password'", text)


if __name__ == "__main__":
    unittest.main()
