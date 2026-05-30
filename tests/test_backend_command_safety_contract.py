from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"


def app_source():
    return APP.read_text(encoding="utf-8")


class MihomoBackendCommandSafetyContractTest(unittest.TestCase):
    def test_crontab_updates_do_not_pipe_through_shell(self):
        text = app_source()

        self.assertIn('subprocess.run(["crontab", "-l"]', text)
        self.assertIn('subprocess.run(["crontab", "-"], input=cron_str', text)
        self.assertNotIn('subprocess.run("crontab -l", shell=True', text)
        self.assertNotIn("echo '{cron_str}' | crontab -", text)

    def test_service_state_uses_argument_based_systemctl(self):
        text = app_source()

        self.assertIn("def is_service_active", text)
        self.assertIn('subprocess.run(["systemctl", "is-active", service]', text)
        self.assertNotIn('subprocess.run("systemctl is-active mihomo", shell=True', text)

    def test_control_actions_use_argument_lists(self):
        text = app_source()

        self.assertIn("control_actions = {", text)
        self.assertIn("'restart': ['systemctl', 'restart', 'mihomo']", text)
        self.assertIn("s, m = run_args(control_actions[action]", text)
        self.assertNotIn("shell=True", text)
        self.assertNotIn("s, m = run_cmd(cmds[action])", text)

    def test_logs_are_read_without_shell_tail(self):
        text = app_source()

        self.assertIn("def read_recent_log_lines", text)
        self.assertNotIn("tail -n 100", text)


if __name__ == "__main__":
    unittest.main()
