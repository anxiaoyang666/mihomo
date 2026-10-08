"""规则同步块渲染的功能测试。

其他测试只 grep 源码，抓不到"生成的 YAML 缩进不对"这类逻辑错误，
这里真的调用 render_sync_blocks 检查输出。不依赖 PyYAML，用缩进一致性来判断。
"""
from pathlib import Path
import sys
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
TEMPLATE = ROOT / "remote-root" / "etc" / "mihomo" / "templates" / "default.yaml"

RULES = {"force-cn": "a.cn\nb.cn\n", "force-nocn": "openai.com\n"}


def load_app(tmp_dir):
    """导入 app.py，但把 Flask 换成桩、把 /etc/mihomo 指到临时目录。"""
    flask = types.ModuleType("flask")

    class FakeFlask:
        permanent_session_lifetime = None
        secret_key = None

        def __init__(self, *args, **kwargs):
            self.config = {}

        def route(self, *args, **kwargs):
            return lambda func: func

    for name in ("render_template", "request", "jsonify", "Response", "redirect", "session"):
        setattr(flask, name, None)
    flask.Flask = FakeFlask
    sys.modules["flask"] = flask

    source = APP.read_text(encoding="utf-8").replace('MIHOMO_DIR = "/etc/mihomo"', f'MIHOMO_DIR = "{tmp_dir}"')
    module = types.ModuleType("mihomo_app_under_test")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


def indent_of(line):
    return line[: len(line) - len(line.lstrip(" "))]


def lines_under_key(text, key):
    """返回顶层 key: 之后、下一个顶层 key 之前的非空非注释行。"""
    out = []
    seen = False
    for line in text.splitlines():
        if line.startswith(f"{key}:"):
            seen = True
            continue
        if not seen:
            continue
        if line and line[0].isalpha():  # 下一个顶层 key，0 缩进的 "- item" 不算
            break
        if line.strip():
            out.append(line)
    return out


class RuleSyncRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.template = TEMPLATE.read_text(encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def assert_block_matches_existing_indent(self, text):
        body = lines_under_key(text, "rules")
        synced = [l for l in body if "DOMAIN-SUFFIX," in l]
        existing = [l for l in body if "DOMAIN-SUFFIX," not in l and self.app.RULE_SYNC_BEGIN not in l and self.app.RULE_SYNC_END not in l]
        self.assertTrue(synced, "没有写入同步规则")
        self.assertTrue(existing, "模板里应有原本的规则")
        self.assertEqual({indent_of(l) for l in synced}, {indent_of(existing[0])}, "同步规则缩进必须和已有列表项一致，否则 YAML 无效")

    def test_default_template_uses_two_space_items(self):
        text = self.app.render_sync_blocks(self.template, RULES)
        self.assert_block_matches_existing_indent(text)
        self.assertIn("  - DOMAIN-SUFFIX,a.cn,DIRECT\n", text)
        self.assertIn("    - +.a.cn\n", text)

    def test_zero_indent_list_style(self):
        flat = self.template.replace("\n  - ", "\n- ")
        text = self.app.render_sync_blocks(flat, RULES)
        self.assert_block_matches_existing_indent(text)
        self.assertIn("\n- DOMAIN-SUFFIX,a.cn,DIRECT\n", text)

    def test_rerender_replaces_block_without_duplicating(self):
        first = self.app.render_sync_blocks(self.template, RULES)
        second = self.app.render_sync_blocks(first, {"force-cn": "c.cn\n", "force-nocn": ""})
        self.assertNotIn("a.cn", second)
        self.assertEqual(second.count(self.app.RULE_SYNC_BEGIN), 1)
        self.assertEqual(second.count(self.app.FAKE_IP_FILTER_BEGIN), 1)
        self.assertEqual(self.app.read_mihomo_sync_rules(second), {"force-cn": "c.cn\n", "force-nocn": ""})

    def test_reapply_carries_rules_into_regenerated_config(self):
        old = Path(self.tmp.name) / "old.yaml"
        new = Path(self.tmp.name) / "new.yaml"
        old.write_text(self.app.render_sync_blocks(self.template, RULES), encoding="utf-8")
        new.write_text(self.template, encoding="utf-8")
        self.assertTrue(self.app.reapply_sync_blocks(str(old), str(new)))
        self.assertEqual(self.app.read_mihomo_sync_rules(new.read_text(encoding="utf-8")), RULES)

        old.write_text(self.template, encoding="utf-8")
        self.assertFalse(self.app.reapply_sync_blocks(str(old), str(new)), "旧配置没有同步规则时应跳过")


if __name__ == "__main__":
    unittest.main()
