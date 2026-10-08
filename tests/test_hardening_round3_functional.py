"""第三轮（低危项）加固的功能测试：真的执行 app.py 里的函数和几个 shell 脚本。

模式和 test_hardening_round2_functional.py 一样：Flask 用桩、/etc/mihomo 指到临时目录。
覆盖：config_value 只认顶层键、proxy_policy_name 只认真实策略组、.env 解析/写入的各种引用形式、
envutil.sh 与 install.sh write_env_line 的输出能被 app.py 读回、gateway_init.sh 的 GATEWAY_AUTOFIX 开关、
update_geo.sh 的"没变不重启 / 全失败报错"逻辑、会话 30 天。
"""
from datetime import timedelta
from pathlib import Path
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
SCRIPTS = ROOT / "remote-root" / "etc" / "mihomo" / "scripts"
TEMPLATE = ROOT / "remote-root" / "etc" / "mihomo" / "templates" / "default.yaml"
INSTALL = ROOT / "install.sh"


def load_app(tmp_dir):
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
    module = types.ModuleType("mihomo_app_under_test_round3")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


def run_bash(script, env=None, cwd=None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=merged, cwd=cwd, timeout=60)


TRICKY_VALUES = {
    "WITH_HASH": "abc#def",
    "WITH_SPACES": "hello world  twice",
    "WITH_SQUOTE": "it's here",
    "WITH_DQUOTE": 'say "hi"',
    "WITH_DOLLAR": "$HOME and `id`",
    "EMPTY": "",
}


class AppFunctionalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = load_app(self.tmp.name)
        for key in ("WEB_USER", "WEB_SECRET", "WEB_SESSION_SECRET", "MIHOMO_API_SECRET", "MIHOMO_CONTROLLER"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()

    # --- config_value ---
    def test_config_value_only_matches_top_level_keys(self):
        (self.dir / "config.yaml").write_text(textwrap.dedent("""\
            proxies:
              - name: node
                type: ss
                secret: nested-should-not-win
            external-controller: 0.0.0.0:9090   # comment
            secret: "top-level"
            """), encoding="utf-8")
        self.assertEqual(self.app.config_value("secret"), "top-level")
        self.assertEqual(self.app.config_value("external-controller"), "0.0.0.0:9090")
        self.assertEqual(self.app.mihomo_controller_settings()["secret"], "top-level")

        (self.dir / "config.yaml").write_text("proxies:\n  - secret: nested\n", encoding="utf-8")
        self.assertEqual(self.app.config_value("secret"), "")
        self.assertEqual(self.app.config_value("missing"), "")

    # --- proxy_policy_name ---
    def test_proxy_policy_name_ignores_comments_and_picks_real_group(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(self.app.proxy_policy_name(template), "♻️ 自动选择")
        self.assertIn("🚀 默认代理", self.app.proxy_group_names(template))

        # 注释和规则行里提到的组名不算数：只有 proxy-groups 里真实存在的条目才可选
        no_auto = textwrap.dedent("""\
            # 以前这里有 ♻️ 自动选择 和 🚀 默认代理
            proxy-groups:
              - {name: 📹 YouTube, type: select, proxies: [🚀 默认代理, 直连]}
              - {name: 🍀 Google, type: select, proxies: [直连]}
            rules:
              - DOMAIN-SUFFIX,example.com,♻️ 自动选择
            """)
        self.assertEqual(self.app.proxy_group_names(no_auto), ["📹 YouTube", "🍀 Google"])
        self.assertEqual(self.app.proxy_policy_name(no_auto), "📹 YouTube")

        block_style = textwrap.dedent("""\
            rules:
              - MATCH,Final   # a rule mentioning Final is not a group
            proxy-groups:
              - name: "My Group"
                type: select
              - name: 'Final'
                type: select
            """)
        self.assertEqual(self.app.proxy_group_names(block_style), ["My Group", "Final"])
        self.assertEqual(self.app.proxy_policy_name(block_style), "Final")
        self.assertEqual(self.app.proxy_policy_name("rules:\n  - MATCH,GLOBAL\n"), "PROXY")

    # --- .env 解析 ---
    def test_parse_env_line_handles_every_quoting_form(self):
        parse = self.app.parse_env_line
        self.assertEqual(parse("A=abc#def\n"), ("A", "abc#def"))
        self.assertEqual(parse("A='abc#def' # trailing comment"), ("A", "abc#def"))
        self.assertEqual(parse('A="hello world"'), ("A", "hello world"))
        self.assertEqual(parse("A='hello world'"), ("A", "hello world"))
        self.assertEqual(parse("A=hello\\ world"), ("A", "hello world"))
        self.assertEqual(parse("A='it'\"'\"'s'"), ("A", "it's"))
        self.assertEqual(parse("A=$'line1\\nline2\\'q\\x41'"), ("A", "line1\nline2'qA"))
        self.assertEqual(parse("A=$'tab\\there'"), ("A", "tab\there"))
        # 引号不配对：退回简单切分并去一层引号
        self.assertEqual(parse("A='unbalanced"), ("A", "'unbalanced"))
        self.assertEqual(parse('A="x"'), ("A", "x"))
        self.assertEqual(parse("export A=1"), ("A", "1"))
        self.assertEqual(parse("A="), ("A", ""))
        self.assertIsNone(parse("# A=1"))
        self.assertIsNone(parse("1A=1"))
        self.assertIsNone(parse("no equals"))

    def test_write_env_round_trips_tricky_values_and_bash_agrees(self):
        self.app.write_env(TRICKY_VALUES)
        env = self.app.read_env()
        for key, value in TRICKY_VALUES.items():
            self.assertEqual(env[key], value, key)
        # bash source 出来的值要和 Python 读到的一致
        for key, value in TRICKY_VALUES.items():
            out = run_bash(f'set -e; source "{self.dir / ".env"}"; printf "%s" "${key}"')
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout, value, key)

    # --- 会话 ---
    def test_session_lifetime_is_thirty_days(self):
        self.assertEqual(self.app.app.permanent_session_lifetime, timedelta(days=30))


class EnvUtilShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env_file = self.dir / ".env"
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def envutil(self, body):
        return run_bash(f'set -e; source "{SCRIPTS / "envutil.sh"}"; {body}', env={"ENV_FILE": str(self.env_file)})

    def test_upsert_env_quotes_sets_0600_and_get_env_reads_back(self):
        self.env_file.write_text("# header\nKEEP=1\nWITH_HASH=old\n", encoding="utf-8")
        # 值通过环境变量传入，避免测试自己再做一层转义
        for key, value in TRICKY_VALUES.items():
            out = run_bash(f'set -e; source "{SCRIPTS / "envutil.sh"}"; upsert_env "{key}" "$VALUE"',
                           env={"ENV_FILE": str(self.env_file), "VALUE": value})
            self.assertEqual(out.returncode, 0, out.stderr)

        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# header\nKEEP=1\n"), "其他行必须原样保留")
        self.assertEqual(text.count("WITH_HASH="), 1, "同名键要替换而不是追加")
        self.assertEqual([p.name for p in self.dir.glob(".env.*.tmp")], [])

        # app.py 读回、get_env 读回、bash source 读回三者一致
        env = self.app.read_env()
        for key, value in TRICKY_VALUES.items():
            self.assertEqual(env[key], value, key)
            out = self.envutil(f'get_env "{key}"')
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout, value, key)
            out = run_bash(f'set -e; source "{self.env_file}"; printf "%s" "${key}"')
            self.assertEqual(out.stdout, value, key)

    def test_get_env_understands_legacy_printf_q_form(self):
        self.env_file.write_text("A=$'line1\\nline2'\nB=abc#def\n", encoding="utf-8")
        self.assertEqual(self.envutil("get_env A").stdout, "line1\nline2")
        self.assertEqual(self.envutil("get_env B").stdout, "abc#def")
        self.assertEqual(self.app.read_env(), {"A": "line1\nline2", "B": "abc#def"})

    def test_upsert_env_rejects_bad_key(self):
        # load_app 导入时已经写入了 WEB_SESSION_SECRET，所以文件存在；要求的是内容不被动
        before = self.env_file.read_text(encoding="utf-8")
        out = self.envutil('upsert_env "bad key" x')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("非法的键名", out.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.dir.glob(".env.*.tmp")], [])


class InstallerEnvWriterTest(unittest.TestCase):
    def test_write_env_line_output_is_readable_by_app_and_bash(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = load_app(tmp.name)
        install = INSTALL.read_text(encoding="utf-8")
        func = re.search(r"(?ms)^write_env_line\(\) \{.*?^\}", install).group(0)
        for key, value in TRICKY_VALUES.items():
            out = run_bash(f'{func}\nwrite_env_line "{key}" "$VALUE"', env={"VALUE": value})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(app.parse_env_line(out.stdout), (key, value))
            self.assertNotIn("$'", out.stdout)
            sourced = run_bash(f'set -e; eval "$LINE"; printf "%s" "${key}"', env={"LINE": out.stdout})
            self.assertEqual(sourced.stdout, value, key)


class GatewayInitTest(unittest.TestCase):
    def test_check_is_a_noop_when_autofix_disabled(self):
        # PATH 指向空目录：开关必须在任何外部命令之前生效，否则这里会报 command not found
        out = run_bash(f'exec /bin/bash "{SCRIPTS / "gateway_init.sh"}" check',
                       env={"GATEWAY_AUTOFIX": "false", "PATH": "/nonexistent"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertEqual(out.stderr, "")


class UpdateGeoTest(unittest.TestCase):
    """用假的 wget / systemctl / curl / notify.sh 跑真实的 update_geo.sh。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.mihomo = self.dir / "mihomo"
        (self.mihomo / "scripts").mkdir(parents=True)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "calls.log"
        (self.mihomo / ".env").write_text("MIHOMO_API_SECRET='s3cret'\n", encoding="utf-8")
        (self.mihomo / "config.yaml").write_text(textwrap.dedent("""\
            external-controller: 0.0.0.0:9090
            secret: ""
            rule-providers:
              cn_ip: {type: http, url: "https://example/cn.mrs"}
              geolocation-!cn:
                type: http
                url: "https://example/geolocation.mrs"
            rules:
              - MATCH,DIRECT
            """), encoding="utf-8")
        self.write_shim("notify.sh", 'printf "notify %s | %s\\n" "$1" "$2" >> "$CALLS_LOG"\n', scripts=True)
        self.write_shim("wget", textwrap.dedent("""\
            out=""
            while [ $# -gt 0 ]; do
              case "$1" in -O) out="$2"; shift ;; esac
              shift
            done
            printf "wget %s\\n" "$out" >> "$CALLS_LOG"
            [ "$FAKE_WGET_MODE" = "ok" ] || exit 1
            printf "%s-%s" "$FAKE_WGET_CONTENT" "$(basename "$out")" > "$out"
            """))
        self.write_shim("systemctl", textwrap.dedent("""\
            printf "systemctl %s\\n" "$*" >> "$CALLS_LOG"
            if [ "$1" = "is-active" ]; then [ "$FAKE_ACTIVE" = "1" ]; exit $?; fi
            exit 0
            """))
        self.write_shim("curl", 'printf "curl %s\\n" "${@: -1}" >> "$CALLS_LOG"; printf "204"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def write_shim(self, name, body, scripts=False):
        path = (self.mihomo / "scripts" / name) if scripts else (self.bin / name)
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def run_geo(self, mode, content="v1", active="1"):
        self.log.write_text("", encoding="utf-8")
        out = run_bash(f'exec /bin/bash "{SCRIPTS / "update_geo.sh"}"', env={
            "MIHOMO_DIR": str(self.mihomo),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "CALLS_LOG": str(self.log),
            "FAKE_WGET_MODE": mode,
            "FAKE_WGET_CONTENT": content,
            "FAKE_ACTIVE": active,
        })
        return out, self.log.read_text(encoding="utf-8")

    def test_all_downloads_failing_notifies_and_exits_nonzero_without_restart(self):
        out, calls = self.run_geo("fail")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("notify ❌ Geo 更新失败", calls)
        self.assertNotIn("systemctl restart", calls)
        self.assertEqual(sorted(p.name for p in self.mihomo.glob("geo*")), [])

    def test_changed_files_restart_once_and_providers_are_refreshed(self):
        out, calls = self.run_geo("ok", content="v1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(sorted(p.name for p in self.mihomo.glob("geo*")), ["geoip.dat", "geoip.metadb", "geosite.dat"])
        self.assertEqual((self.mihomo / "geosite.dat").read_text(encoding="utf-8"), "v1-geosite.dat")
        self.assertEqual(calls.count("systemctl restart mihomo"), 1)
        self.assertNotIn("notify", calls)
        # rule-providers 通过控制器 API 刷新，名字里的 ! 要 URL 编码，0.0.0.0 要换成 127.0.0.1
        self.assertIn("curl http://127.0.0.1:9090/providers/rules/cn_ip", calls)
        self.assertIn("curl http://127.0.0.1:9090/providers/rules/geolocation-%21cn", calls)

        # 内容没变：不替换、不重启
        out, calls = self.run_geo("ok", content="v1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("systemctl restart", calls)
        self.assertIn("无变化", out.stdout)

        # 内核没在跑：跳过刷新但 Geo 文件照常更新
        out, calls = self.run_geo("ok", content="v2", active="0")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("curl", calls)
        self.assertEqual(calls.count("systemctl restart mihomo"), 1)
        self.assertEqual((self.mihomo / "geoip.dat").read_text(encoding="utf-8"), "v2-geoip.dat")


if __name__ == "__main__":
    unittest.main()
