"""配置编辑器：提交的内容和当前 config.yaml 一样时不校验、不写、不备份，前端也不重启。"""
from pathlib import Path
import tempfile
import types
import unittest

from test_rule_sync_render import TEMPLATE, load_app


class ConfigEditorUnchangedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        app = self.app = load_app(self.tmp.name)
        app.session = {"logged_in": True}
        app.jsonify = lambda payload: payload
        self.validated = []
        app.validate_config = lambda path: self.validated.append(path) or (True, "ok")
        # 保存后的热加载/重启由 test_config_hot_reload 覆盖，这里不碰真实控制器和 systemctl
        app.apply_config_change = lambda old, new: (True, "已热加载，未中断连接", "reloaded")
        self.original = TEMPLATE.read_text(encoding="utf-8")
        (self.dir / "config.yaml").write_text(self.original, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, content):
        self.app.request = types.SimpleNamespace(
            method="POST",
            path="/api/config",
            headers={"X-Requested-With": "XMLHttpRequest"},
            get_json=lambda silent=True: {"content": content},
        )
        return self.app.handle_config()

    def test_identical_content_is_a_no_op(self):
        config = self.dir / "config.yaml"
        mtime = config.stat().st_mtime_ns
        # 只差换行符和末尾换行也算没变
        for content in (self.original, self.original.replace("\n", "\r\n"), self.original.rstrip("\n")):
            result = self.post(content)
            self.assertEqual(result, {"success": True, "unchanged": True, "message": "配置内容没有变化，未重启"})
        self.assertEqual(self.validated, [])
        self.assertEqual(config.stat().st_mtime_ns, mtime)
        self.assertEqual(config.read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.dir / "backup").exists())
        self.assertEqual([p.name for p in self.dir.glob("config.yaml.*")], [])

    def test_changed_content_is_validated_written_and_backed_up(self):
        changed = self.original + "# edited\n"
        result = self.post(changed)
        self.assertTrue(result["success"], result)
        self.assertNotIn("unchanged", result)
        self.assertEqual(len(self.validated), 1)
        self.assertEqual((self.dir / "config.yaml").read_text(encoding="utf-8"), changed)
        self.assertEqual(len(list((self.dir / "backup").glob("config_*.yaml"))), 1)
        # 只改缩进/空白也算变化：不重排 YAML
        self.assertNotIn("unchanged", self.post(changed.replace("\n  ", "\n    ", 1)))


if __name__ == "__main__":
    unittest.main()
