"""账户安全页：安全检查只给结论，不返回密码 / 密钥；改账号时当前密码用恒定时间比较。"""
from pathlib import Path
import json
import tempfile
import unittest

from test_rule_sync_render import ROOT, load_app

APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"


class SecurityStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, password, controller, secret):
        self.app.web_credentials = lambda: ("admin", password)
        values = {"external-controller": controller, "secret": secret}
        self.app.config_value = lambda key: values.get(key)
        return self.app.security_status()

    def test_weak_and_exposed(self):
        res = self.status("123456", "0.0.0.0:9090", "1234")
        self.assertFalse(res["password_strong"])
        self.assertTrue(res["controller_exposed"])
        self.assertTrue(res["controller_secret_set"])
        self.assertFalse(res["controller_secret_strong"])
        self.assertNotIn("1234", json.dumps(res), "不能把密钥带出去")
        self.assertNotIn("123456", json.dumps(res))

    def test_strong_and_local(self):
        res = self.status("correct-horse-9", "127.0.0.1:9090", "")
        self.assertTrue(res["password_strong"])
        self.assertFalse(res["controller_exposed"])

    def test_account_update_uses_constant_time_compare(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn('secrets.compare_digest(current_password.encode("utf-8"), valid_pass.encode("utf-8"))', source)
        self.assertIn("@app.route(\"/api/security-status\")\n@login_required", source)


if __name__ == "__main__":
    unittest.main()
