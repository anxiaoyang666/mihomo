from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"


def text(path):
    return path.read_text(encoding="utf-8")


class MihomoAccountCredentialsContractTest(unittest.TestCase):
    def test_backend_exposes_guarded_account_update_endpoint(self):
        source = text(APP)

        self.assertIn("@app.route('/api/account', methods=['POST'])", source)
        self.assertIn("def update_account_credentials", source)
        self.assertIn("current_password = str(data.get('current_password') or '')", source)
        self.assertIn("if current_password != valid_pass", source)
        self.assertIn("is_valid_web_username", source)
        self.assertIn('"WEB_USER": new_user', source)
        self.assertIn('"WEB_SECRET": new_password', source)
        self.assertIn("write_env(updates)", source)
        self.assertIn("session.clear()", source)

    def test_frontend_has_account_security_form(self):
        source = text(INDEX)

        self.assertIn("账户安全", source)
        self.assertIn('id="account_user"', source)
        self.assertIn('id="account_current_password"', source)
        self.assertIn('id="account_new_password"', source)
        self.assertIn('id="account_confirm_password"', source)
        self.assertIn("function saveAccount", source)
        self.assertIn("api('/account'", source)


if __name__ == "__main__":
    unittest.main()
