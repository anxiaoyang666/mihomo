from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "remote-root"
APP = REMOTE / "etc" / "mihomo" / "manager" / "app.py"
INDEX = REMOTE / "etc" / "mihomo" / "manager" / "templates" / "index.html"
STATIC = REMOTE / "etc" / "mihomo" / "manager" / "static"
SCRIPTS = REMOTE / "etc" / "mihomo" / "scripts"
SYSTEMD = REMOTE / "etc" / "systemd" / "system"
CLI = REMOTE / "usr" / "bin" / "mihomo"
INSTALL = ROOT / "install.sh"


def text(path):
    return path.read_text(encoding="utf-8")


class MihomoFinalHardeningContractTest(unittest.TestCase):
    def test_release_version_is_019(self):
        self.assertIn('PANEL_VERSION = "0.1.10"', text(APP))

    def test_frontend_uses_local_vendor_assets(self):
        index = text(INDEX)

        self.assertIn('/static/vendor/bootstrap.min.css', index)
        self.assertIn('/static/vendor/bootstrap-icons.css', index)
        self.assertIn('/static/vendor/bootstrap.bundle.min.js', index)
        self.assertNotIn('https://cdn.jsdelivr.net', index)
        self.assertTrue((STATIC / "vendor" / "bootstrap.min.css").is_file())
        self.assertTrue((STATIC / "vendor" / "bootstrap-icons.css").is_file())
        self.assertTrue((STATIC / "vendor" / "bootstrap.bundle.min.js").is_file())
        self.assertTrue((STATIC / "vendor" / "fonts" / "bootstrap-icons.woff2").is_file())

    def test_installer_writes_shell_escaped_env(self):
        install = text(INSTALL)

        self.assertIn("write_env_line()", install)
        self.assertIn("printf '%s=%q\\n'", install)
        self.assertIn('write_env_line "WEB_SESSION_SECRET" "$session_secret"', install)
        self.assertNotIn('WEB_SESSION_SECRET="$session_secret"', install)
        self.assertNotIn('cat > "$INSTALL_DIR/.env" <<EOF', install)

    def test_legacy_env_writes_use_safe_upsert(self):
        for path in [CLI, SCRIPTS / "manage_config.sh", SCRIPTS / "set_notify.sh"]:
            source = text(path)
            with self.subTest(path=path.name):
                self.assertIn("upsert_env()", source)
                self.assertIn("shlex.quote", source)
                self.assertNotIn('sed -i "s|^SUB_URL=.*|SUB_URL=\\"$url\\"|"', source)
                self.assertNotIn('sed -i "s|^NOTIFY_URL=.*|NOTIFY_URL=\\"$url\\"|"', source)

    def test_uninstall_uses_temp_crontab_cleanup(self):
        uninstall = text(SCRIPTS / "uninstall.sh")

        self.assertIn('TMP_DIR="$(mktemp -d)"', uninstall)
        self.assertIn("trap cleanup EXIT", uninstall)
        self.assertIn('grep -F -v -- "gateway_init.sh"', uninstall)
        self.assertIn('crontab "$TMP_CRON"', uninstall)
        self.assertNotIn('| crontab -', uninstall)

    def test_systemd_units_avoid_strict_hardening_for_compatibility(self):
        for name in ["mihomo.service", "mihomo-manager.service", "force-ip-forward.service"]:
            unit = text(SYSTEMD / name)
            with self.subTest(unit=name):
                self.assertNotIn("NoNewPrivileges=true", unit)
                self.assertNotIn("PrivateTmp=true", unit)
                self.assertNotIn("UMask=0077", unit)

        self.assertIn("RestartSec=3", text(SYSTEMD / "mihomo.service"))
        self.assertIn("RestartSec=3", text(SYSTEMD / "mihomo-manager.service"))
        self.assertNotIn("NoNewPrivileges=true", text(SCRIPTS / "service_ctl.sh"))


if __name__ == "__main__":
    unittest.main()
