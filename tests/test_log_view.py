"""运行日志页：服务端按来源 / 级别 / 关键字筛选（范围是日志末尾 4MB），账户安全是单独的页面。"""
from pathlib import Path
import tempfile
import unittest

from test_rule_sync_render import ROOT, load_app

INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"


class LogViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        core = Path(self.tmp.name) / "mihomo.log"
        lines = [f'time="2026-10-09T05:00:{i % 60:02d}Z" level=info msg="[TCP] conn {i}"' for i in range(1000)]
        lines.insert(10, 'time="2026-10-09T04:00:00Z" level=warning msg="dial DIRECT error: \\"x\\""')
        core.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.app.LOG_SOURCES = dict(self.app.LOG_SOURCES, core=str(core), geo=self.tmp.name + "/missing.log")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_latest_lines(self):
        text, note = self.app.read_log_view("core")
        self.assertEqual(len(text.splitlines()), self.app.LOG_VIEW_LINES)
        self.assertTrue(text.endswith("conn 999\""))
        self.assertEqual(note, "")

    def test_level_and_search_cover_whole_tail(self):
        text, _ = self.app.read_log_view("core", level="warn")
        self.assertEqual(len(text.splitlines()), 1, "第 10 行的警告也能筛出来，不止最后 100 行")
        text, note = self.app.read_log_view("core", query="CONN 1")
        self.assertIn("conn 1\"", text)
        self.assertEqual(len(text.splitlines()), 111, "不分大小写：conn 1、10-19、100-199")
        _, note = self.app.read_log_view("core", query="conn")
        self.assertEqual(note, "共找到 1000 行，显示最新 300 行")

    def test_unknown_and_missing_sources(self):
        self.assertEqual(self.app.read_log_view("../../etc/passwd"), ("", "未知日志"))
        self.assertEqual(self.app.read_log_view("geo"), ("", "日志还没有生成"))

    def test_account_has_its_own_tab(self):
        html = INDEX.read_text(encoding="utf-8")
        account = html.index('id="tab-account"')
        self.assertGreater(html.index('id="account_user"'), account)
        notify = html.index('id="tab-notify"')
        self.assertNotIn('id="account_user"', html[notify:html.index('</section>', notify)])
        self.assertIn('id="tab-account-btn"', html)


if __name__ == "__main__":
    unittest.main()
