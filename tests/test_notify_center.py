"""通知中心：最近通知列表、测试按钮按实际发送结果提示、改了没保存先保存再测。"""
from pathlib import Path
import tempfile
import unittest

from test_rule_sync_render import ROOT, load_app

INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"

LOG = """\
[2026-10-06 23:56:41] Webhook failed title=♻️ 订阅更新成功 exit=35 http=000 error=curl: (35) SSL connect error response=
[2026-10-09 08:52:38] Webhook sent title=🔔 联通 · 通知测试 http=200
garbage line
[2026-10-09 10:33:05] Webhook sent title=✅ 联通 · 订阅配置已更新 http=200
[2026-10-09 11:00:00] Webhook failed title=⚠️ 联通 · Geo 数据更新失败 exit=0 http=502 response=bad gateway
"""


class NotifyCenterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.log = Path(self.tmp.name) / "notify.log"
        self.log.write_text(LOG, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_recent_notifications(self):
        items = self.app.recent_notifications(limit=3, path=str(self.log))
        self.assertEqual([i["title"] for i in items], ["⚠️ 联通 · Geo 数据更新失败", "✅ 联通 · 订阅配置已更新", "🔔 联通 · 通知测试"])
        self.assertFalse(items[0]["ok"])
        self.assertEqual(items[0]["error"], "exit=0 http=502")
        self.assertTrue(items[1]["ok"])
        oldest = self.app.recent_notifications(path=str(self.log))[-1]
        self.assertEqual(oldest["error"], "curl: (35) SSL connect error")

    def test_missing_log(self):
        self.assertEqual(self.app.recent_notifications(path=self.tmp.name + "/nope.log"), [])

    def test_test_button_reports_real_result(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn('s = s and "已发送" in m and "发送失败" not in m', source)
        self.assertIn("@app.route('/api/notify-log')\n@login_required", source)
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('onclick="testNotify(this)"', html)
        self.assertIn("if (notifyFieldsDirty()) {", html)
        self.assertIn("rememberNotifyFields();", html)


if __name__ == "__main__":
    unittest.main()
