"""分流同步的域名规则：和 mosctl（mosdns domain_set）的写法对齐，写错的行保存时报错，不再悄悄丢掉。"""
import tempfile
import unittest

from test_rule_sync_render import load_app


class RuleDomainFormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = load_app(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_mosctl_style_lines_are_understood(self):
        content = "domain:Foo.com\nfull:bar.com\nbaz.com # 备注\n# 整行注释\n.qux.com\nDOMAIN-SUFFIX,a.cn\nkeyword:abc\nregexp:^x$\n"
        domains, problems = self.app.parse_rule_domains(content)
        self.assertEqual(domains, ["foo.com", "bar.com", "baz.com", "qux.com", "a.cn"])
        self.assertEqual([(n, k) for n, _i, k in problems], [(7, "mosdns_only"), (8, "mosdns_only")])

    def test_save_check_reports_mistakes(self):
        check = self.app.rule_content_problem
        self.assertIn("不能有空格", check("a.com b.com")[0])
        error = check("ok.com\nhttps://www.example.com/path")[0]
        self.assertIn("第 2 行是网址", error)
        self.assertIn("www.example.com", error)
        self.assertIn("不是合法域名", check("localhost")[0])
        self.assertEqual(check("a.com\nkeyword:abc"), ("", "第 2 行是 keyword:/regexp: 规则，mihomo 不支持，只在 mosdns 生效"))
        self.assertEqual(check("a.com # x\n"), ("", ""))

    def test_save_rule_content_blocks_mistakes(self):
        ok, message, action = self.app.save_rule_content("force-cn", "good.com\nbad domain.com")
        self.assertFalse(ok)
        self.assertIn("未保存", message)
        self.assertIsNone(action)


if __name__ == "__main__":
    unittest.main()
