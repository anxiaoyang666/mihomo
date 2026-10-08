"""规则同步块渲染与应用的功能测试。

其他测试只 grep 源码，抓不到"生成的 YAML 缩进不对"这类逻辑错误，
这里真的调用 render_sync_blocks / save_rule_content / apply_synced_rules 检查输出。
缩进用一致性判断；装了 PyYAML 时再额外整份解析一遍。
"""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from urllib import error as urlerror
from urllib import request as real_urlrequest

try:
    import yaml
except ImportError:  # pragma: no cover - 取决于环境
    yaml = None


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


LEGACY_RULES_BLOCK = (
    "  # MOSCTL_MIHOMO_RULE_SYNC_BEGIN\n"
    "  - DOMAIN-SUFFIX,a.cn,DIRECT\n"
    "  - DOMAIN-SUFFIX,b.cn,DIRECT\n"
    "  - DOMAIN-SUFFIX,openai.com,♻️ 自动选择\n"
    "  # MOSCTL_MIHOMO_RULE_SYNC_END\n"
)
LEGACY_FAKE_IP_BLOCK = (
    "    # MOSCTL_MIHOMO_FAKE_IP_FILTER_BEGIN\n"
    "    - +.a.cn\n"
    "    - +.b.cn\n"
    "    # MOSCTL_MIHOMO_FAKE_IP_FILTER_END\n"
)


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


class RuleSyncRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = load_app(self.tmp.name)
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.puts = []
        self.put_error = None
        self.restarts = []
        self.scheduled = []
        app = self.app

        def fake_urlopen(req, timeout=None):
            self.puts.append((req.get_method(), req.full_url, dict(req.header_items()), timeout))
            if self.put_error is not None:
                raise self.put_error
            return FakeResponse(204)

        app.urlrequest = types.SimpleNamespace(Request=real_urlrequest.Request, urlopen=fake_urlopen)
        app.restart_mihomo = lambda: self.restarts.append(1) or (True, "ok")
        app.schedule_mihomo_restart = lambda: self.scheduled.append(1)
        app.validate_config = lambda path: (True, "skip")

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers ---
    def legacy_config(self):
        text = self.template.replace("rules:\n", "rules:\n" + LEGACY_RULES_BLOCK, 1)
        return text.replace("  fake-ip-filter:\n", "  fake-ip-filter:\n" + LEGACY_FAKE_IP_BLOCK, 1)

    def config_path(self):
        return self.dir / "config.yaml"

    def rule_file(self, rule_id):
        return Path(self.app.RULE_PROVIDER_FILES[rule_id])

    def assert_yaml_valid(self, text):
        if yaml is None:
            return
        data = yaml.safe_load(text)
        providers = data["rule-providers"]
        self.assertEqual(providers["mosctl_force_cn"], {"type": "file", "behavior": "domain", "format": "yaml", "path": "./rules/mosctl-force-cn.yaml"})
        self.assertEqual(providers["mosctl_force_nocn"]["path"], "./rules/mosctl-force-nocn.yaml")
        self.assertEqual(data["rules"][0], "RULE-SET,mosctl_force_cn,DIRECT")
        self.assertTrue(data["rules"][1].startswith("RULE-SET,mosctl_force_nocn,"))
        self.assertIn("rule-set:mosctl_force_cn", data["dns"]["fake-ip-filter"])

    def assert_block_matches_existing_indent(self, text):
        body = lines_under_key(text, "rules")
        synced = [l for l in body if "RULE-SET,mosctl_" in l]
        existing = [l for l in body if "RULE-SET,mosctl_" not in l and self.app.RULE_SYNC_BEGIN not in l and self.app.RULE_SYNC_END not in l]
        self.assertEqual(len(synced), 2, "应写入两条 RULE-SET 同步规则")
        self.assertTrue(existing, "模板里应有原本的规则")
        self.assertEqual({indent_of(l) for l in synced}, {indent_of(existing[0])}, "同步规则缩进必须和已有列表项一致，否则 YAML 无效")

    # --- 渲染 ---
    def test_default_template_uses_two_space_items(self):
        text = self.app.render_sync_blocks(self.template)
        self.assert_block_matches_existing_indent(text)
        self.assertIn("  - RULE-SET,mosctl_force_cn,DIRECT\n", text)
        self.assertIn("    - rule-set:mosctl_force_cn\n", text)
        self.assertNotIn("DOMAIN-SUFFIX", text[text.find(self.app.RULE_SYNC_BEGIN):text.find(self.app.RULE_SYNC_END)])
        self.assertEqual(self.app.sync_blocks_mode(text), "provider")
        self.assert_yaml_valid(text)

    def test_zero_indent_list_style(self):
        flat = self.template.replace("\n  - ", "\n- ")
        text = self.app.render_sync_blocks(flat)
        self.assert_block_matches_existing_indent(text)
        self.assertIn("\n- RULE-SET,mosctl_force_cn,DIRECT\n", text)

    def test_provider_block_matches_existing_rule_providers_indent(self):
        text = self.app.render_sync_blocks(self.template)
        body = lines_under_key(text, "rule-providers")
        ours = [l for l in body if l.lstrip().startswith("mosctl_force_")]
        theirs = [l for l in body if l.lstrip().startswith("google_domain:")]
        self.assertEqual(len(ours), 2)
        self.assertEqual({indent_of(l) for l in ours}, {indent_of(theirs[0])})
        self.assertEqual(text.count("\nrule-providers:"), 1, "不能再追加一个 rule-providers 键")

        # rule-providers 子项改成 4 格缩进，插入的块要跟着用 4 格
        four = "\n".join(("  " + line if inside and line.startswith("  ") else line) for line, inside in self._providers_lines(self.template))
        text = self.app.render_sync_blocks(four)
        ours = [l for l in text.splitlines() if l.lstrip().startswith("mosctl_force_")]
        self.assertEqual({indent_of(l) for l in ours}, {"    "})

    def _providers_lines(self, text):
        inside = False
        for line in text.split("\n"):
            if line.startswith("rule-providers:"):
                inside = True
                yield line, False
                continue
            elif line and not line[0].isspace():
                inside = False
            yield line, inside

    def test_rule_providers_created_when_absent(self):
        minimal = "proxy-groups:\n  - {name: GLOBAL, type: select, proxies: [DIRECT]}\nrules:\n  - MATCH,DIRECT\n"
        text = self.app.render_sync_blocks(minimal)
        self.assertIn("\nrule-providers:\n  # MOSCTL_MIHOMO_RULE_PROVIDERS_BEGIN\n  mosctl_force_cn: {type: file, behavior: domain, format: yaml, path: ./rules/mosctl-force-cn.yaml}\n", text)
        self.assertIn("  - RULE-SET,mosctl_force_nocn,GLOBAL\n", text)
        self.assert_yaml_valid(text)
        empty = self.app.render_sync_blocks(minimal + "rule-providers: {}\n")
        self.assertEqual(empty.count("rule-providers:"), 1)
        self.assertIn("rule-providers:\n  # MOSCTL_MIHOMO_RULE_PROVIDERS_BEGIN\n", empty)

    def test_static_blocks_are_idempotent(self):
        first = self.app.render_sync_blocks(self.template)
        second = self.app.render_sync_blocks(first)
        self.assertEqual(first, second)
        for marker in (self.app.RULE_SYNC_BEGIN, self.app.FAKE_IP_FILTER_BEGIN, self.app.RULE_PROVIDERS_BEGIN):
            self.assertEqual(second.count(marker), 1)

    def test_legacy_inline_blocks_are_replaced(self):
        legacy = self.legacy_config()
        self.assertEqual(self.app.sync_blocks_mode(legacy), "legacy")
        self.assertEqual(self.app.read_mihomo_sync_rules(legacy), RULES)
        text = self.app.render_sync_blocks(legacy)
        self.assertNotIn("DOMAIN-SUFFIX,a.cn", text)
        self.assertNotIn("- +.a.cn", text)
        self.assertEqual(text.count(self.app.FAKE_IP_FILTER_BEGIN), 1)
        self.assert_yaml_valid(text)

    # --- 规则文件 ---
    def test_rule_files_round_trip(self):
        self.app.write_rule_provider_files({"force-cn": "B.cn\na.cn\na.cn\n# c\n*.x.cn\n", "force-nocn": ""})
        cn = self.rule_file("force-cn")
        self.assertEqual(cn.read_text(encoding="utf-8"), "payload:\n  - '+.b.cn'\n  - '+.a.cn'\n  - '+.*.x.cn'\n")
        self.assertEqual(oct(cn.stat().st_mode & 0o777), "0o644")
        self.assertEqual(self.rule_file("force-nocn").read_text(encoding="utf-8"), "payload: []\n")
        self.assertEqual(self.app.read_rule_provider_files(), {"force-cn": "b.cn\na.cn\n*.x.cn\n", "force-nocn": ""})
        if yaml is not None:
            self.assertEqual(yaml.safe_load(cn.read_text(encoding="utf-8")), {"payload": ["+.b.cn", "+.a.cn", "+.*.x.cn"]})
        self.assertEqual([p.name for p in cn.parent.glob("*.tmp")], [])

    # --- 应用 ---
    def test_legacy_save_migrates_once_and_preserves_domains(self):
        self.config_path().write_text(self.legacy_config(), encoding="utf-8")
        ok, message, action = self.app.save_rule_content("force-nocn", "openai.com\nclaude.ai\n")
        self.assertTrue(ok, message)
        self.assertEqual(action, "restart")
        self.assertIn("一次性升级", message)
        self.assertEqual(self.puts, [], "迁移走重启，不调用热刷新")
        config = self.config_path().read_text(encoding="utf-8")
        self.assertEqual(self.app.sync_blocks_mode(config), "provider")
        self.assertEqual(self.app.read_mihomo_sync_rules(), {"force-cn": "a.cn\nb.cn\n", "force-nocn": "openai.com\nclaude.ai\n"})
        self.assertTrue(list((self.dir / "backup").glob("config.before-rule-sync.*.yaml")))
        self.assert_yaml_valid(config)

        ok, message, action = self.app.save_rule_content("force-cn", "c.cn\n")
        self.assertEqual(action, "reloaded", message)
        self.assertEqual(self.app.sync_load_mode_label(), "规则集热加载（无需重启）")

    def test_migration_validation_failure_restores_everything(self):
        legacy = self.legacy_config()
        self.config_path().write_text(legacy, encoding="utf-8")
        self.app.validate_config = lambda path: (False, "bad")
        ok, message, action = self.app.save_rule_content("force-cn", "c.cn\n")
        self.assertFalse(ok)
        self.assertIn("bad", message)
        self.assertEqual(self.config_path().read_text(encoding="utf-8"), legacy)
        self.assertFalse(self.rule_file("force-cn").exists())
        self.assertEqual([p.name for p in self.dir.glob("config.yaml.*")], [])

    def test_provider_form_writes_files_and_hot_refreshes_without_restart(self):
        self.config_path().write_text(self.app.render_sync_blocks(self.template), encoding="utf-8")
        self.app.write_rule_provider_files({"force-cn": "a.cn\n", "force-nocn": ""})
        self.app.write_env({"MIHOMO_CONTROLLER": "127.0.0.1:9090", "MIHOMO_API_SECRET": "s3cret"})
        before = self.config_path().read_text(encoding="utf-8")
        ok, message, action = self.app.save_rule_content("force-cn", "a.cn\nnew.cn\n")
        self.assertTrue(ok, message)
        self.assertEqual(action, "reloaded")
        self.assertEqual(self.config_path().read_text(encoding="utf-8"), before, "config.yaml 不应被改写")
        self.assertIn("+.new.cn", self.rule_file("force-cn").read_text(encoding="utf-8"))
        self.assertEqual(
            [(m, u, t) for m, u, _h, t in self.puts],
            [("PUT", "http://127.0.0.1:9090/providers/rules/mosctl_force_cn", 5), ("PUT", "http://127.0.0.1:9090/providers/rules/mosctl_force_nocn", 5)],
        )
        self.assertEqual(self.puts[0][2].get("Authorization"), "Bearer s3cret")
        self.assertFalse((self.dir / "backup").exists())

        # 收到同步：同样只热刷新，不重启
        self.puts.clear()
        ok, message = self.app.apply_synced_rules({"force-nocn": "openai.com\n"})
        self.assertTrue(ok, message)
        self.assertEqual(len(self.puts), 2)
        self.assertEqual(self.scheduled, [])
        self.assertEqual(self.app.read_mihomo_sync_rules()["force-nocn"], "openai.com\n")

        # 本地保存接口：热加载成功时不重启
        self.puts.clear()
        app = self.app
        app.request = types.SimpleNamespace(host_url="http://127.0.0.1:7838/", method="POST", path="/api/rules/force-cn",
                                            headers={"X-Requested-With": "XMLHttpRequest"}, get_json=lambda silent=True: {"content": "z.cn\n"})
        app.session = {"logged_in": True}
        app.jsonify = lambda payload: payload
        result = app.api_rules("force-cn")
        self.assertTrue(result["success"], result)
        self.assertIn("无需重启", result["message"])
        self.assertEqual(self.restarts, [])

    def test_refresh_failure_falls_back_to_restart(self):
        self.config_path().write_text(self.app.render_sync_blocks(self.template), encoding="utf-8")
        self.put_error = urlerror.URLError("connection refused")
        ok, message, action = self.app.save_rule_content("force-cn", "a.cn\n")
        self.assertTrue(ok)
        self.assertEqual(action, "restart")
        self.assertIn("热加载失败", message)
        self.assertIn("+.a.cn", self.rule_file("force-cn").read_text(encoding="utf-8"))
        ok, message = self.app.apply_synced_rules({"force-cn": "b.cn\n"})
        self.assertTrue(ok)
        self.assertEqual(self.scheduled, [1])
        self.assertIn("后台重启", message)

    # --- 内容没变就跳过 ---
    def test_unchanged_content_skips_everything(self):
        self.config_path().write_text(self.app.render_sync_blocks(self.template), encoding="utf-8")
        self.app.write_rule_provider_files({"force-cn": "a.cn\nb.cn\n", "force-nocn": ""})
        mtime = self.rule_file("force-cn").stat().st_mtime_ns
        ok, message, action = self.app.save_rule_content("force-cn", "# 注释\nB.cn\n\na.cn\na.cn\n")
        self.assertEqual((ok, message, action), (True, "规则内容没有变化，未重启", "unchanged"))
        self.assertEqual(self.app.apply_synced_rules({"force-cn": "b.cn|a.cn"}), (True, "规则内容没有变化，未重启"))
        self.assertEqual(self.puts, [])
        self.assertEqual(self.scheduled, [])
        self.assertEqual(self.rule_file("force-cn").stat().st_mtime_ns, mtime)

    def test_unchanged_legacy_content_does_not_migrate(self):
        legacy = self.legacy_config()
        self.config_path().write_text(legacy, encoding="utf-8")
        self.assertEqual(self.app.apply_synced_rules({"force-cn": "b.cn\na.cn\n"}), (True, "规则内容没有变化，未重启"))
        self.assertEqual(self.config_path().read_text(encoding="utf-8"), legacy)
        self.assertEqual(self.app.sync_load_mode_label(), "旧格式（下次保存时自动升级，需重启一次）")

    def test_api_rules_unchanged_still_broadcasts_without_restart(self):
        app = self.app
        self.config_path().write_text(app.render_sync_blocks(self.template), encoding="utf-8")
        app.write_rule_provider_files({"force-cn": "a.cn\n", "force-nocn": ""})
        app.request = types.SimpleNamespace(host_url="http://127.0.0.1:7838/", method="POST", path="/api/rules/force-cn",
                                            headers={"X-Requested-With": "XMLHttpRequest"}, get_json=lambda silent=True: {"content": "a.cn\n"})
        app.session = {"logged_in": True}
        app.jsonify = lambda payload: payload
        broadcasts = []
        app.start_broadcast = lambda rule_id, content: broadcasts.append((rule_id, content)) or ("job1", "正在后台同步到 1 个节点…")
        result = app.api_rules("force-cn")
        self.assertTrue(result["success"])
        self.assertTrue(result["message"].startswith("规则内容没有变化，未重启；正在后台同步"))
        self.assertEqual(broadcasts, [("force-cn", "a.cn\n")])
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.puts, [])

    # --- 订阅更新 ---
    def test_reapply_carries_rules_into_regenerated_config(self):
        old = self.dir / "old.yaml"
        new = self.dir / "new.yaml"
        old.write_text(self.app.render_sync_blocks(self.template), encoding="utf-8")
        self.app.write_rule_provider_files(RULES)
        new.write_text(self.template, encoding="utf-8")
        self.assertTrue(self.app.reapply_sync_blocks(str(old), str(new)))
        regenerated = new.read_text(encoding="utf-8")
        self.assertEqual(self.app.sync_blocks_mode(regenerated), "provider")
        self.assertEqual(self.app.read_mihomo_sync_rules(regenerated), RULES)
        self.assert_yaml_valid(regenerated)

        old.write_text(self.template, encoding="utf-8")
        new.write_text(self.template, encoding="utf-8")
        self.assertFalse(self.app.reapply_sync_blocks(str(old), str(new)), "旧配置没有同步规则时应跳过")
        self.assertEqual(new.read_text(encoding="utf-8"), self.template)

    def test_reapply_migrates_legacy_source_into_files(self):
        old = self.dir / "old.yaml"
        new = self.dir / "new.yaml"
        old.write_text(self.legacy_config(), encoding="utf-8")
        new.write_text(self.template, encoding="utf-8")
        self.assertTrue(self.app.reapply_sync_blocks(str(old), str(new)))
        self.assertEqual(self.app.read_rule_provider_files(), {"force-cn": "a.cn\nb.cn\n", "force-nocn": "openai.com\n"})
        self.assertEqual(self.app.sync_blocks_mode(new.read_text(encoding="utf-8")), "provider")


if __name__ == "__main__":
    unittest.main()
