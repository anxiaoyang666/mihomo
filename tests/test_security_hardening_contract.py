from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
SCRIPTS = ROOT / "remote-root" / "etc" / "mihomo" / "scripts"
CLI = ROOT / "remote-root" / "usr" / "bin" / "mihomo"


def source(path):
    return path.read_text(encoding="utf-8")


class MihomoSecurityHardeningContractTest(unittest.TestCase):
    def test_env_writer_uses_shell_safe_quoting(self):
        text = source(APP)

        self.assertIn("import shlex", text)
        self.assertIn("def parse_env_line", text)
        self.assertIn("def env_value_for_shell", text)
        self.assertIn("shlex.quote", text)
        self.assertNotIn('f\'{k}="{updates[k]}"', text)

    def test_panel_upgrade_uses_safe_zip_extraction(self):
        text = source(APP)

        self.assertIn("def safe_extract_zip", text)
        self.assertIn("os.path.abspath", text)
        self.assertIn("archive.extract(member, destination)", text)
        self.assertNotIn("archive.extractall(tmpdir)", text)

    def test_scripts_use_unique_temp_paths_for_downloads(self):
        script_names = [
            "update_subscription.sh",
            "install_kernel.sh",
            "manage_config.sh",
            "manage_ui.sh",
            "notify.sh",
            "apply_settings.sh",
        ]

        for name in script_names:
            text = source(SCRIPTS / name)
            with self.subTest(script=name):
                self.assertIn("mktemp", text)
                self.assertIn("trap", text)

        cli_text = source(CLI)
        self.assertIn("mktemp", cli_text)
        self.assertIn("trap", cli_text)


if __name__ == "__main__":
    unittest.main()
