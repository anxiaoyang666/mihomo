"""通知重做：统一标题 "{图标} {站点名} · {主题}"、纯文本正文、SITE_NAME、重复失败去重、各调用点都走新格式。"""
from pathlib import Path
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest

from test_rule_sync_render import load_app

ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "remote-root" / "etc" / "mihomo"
MANAGER = MIHOMO / "manager"
SCRIPTS = MIHOMO / "scripts"
FORMAT = MANAGER / "notify_format.py"
APP = MANAGER / "app.py"
AUTO_UPDATE = MANAGER / "auto_update.py"
INDEX = MANAGER / "templates" / "index.html"
CLI = ROOT / "remote-root" / "usr" / "bin" / "mihomo"
DAY = 86400
NOW = 1_800_000_000


def load_format():
    spec = importlib.util.spec_from_file_location("notify_format_under_test", FORMAT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(path):
    return Path(path).read_text(encoding="utf-8")


class TitleBuilderTest(unittest.TestCase):
    def setUp(self):
        self.nf = load_format()

    def test_icons_per_level(self):
        self.assertEqual(self.nf.build_title("ok", "mihomo 内核已更新", "联通"), "✅ 联通 · mihomo 内核已更新")
        self.assertEqual(self.nf.build_title("warn", "订阅节点刷新失败", "熙国"), "⚠️ 熙国 · 订阅节点刷新失败")
        self.assertEqual(self.nf.build_title("fail", "订阅配置应用失败，已回滚", "工厂"), "❌ 工厂 · 订阅配置应用失败，已回滚")
        self.assertEqual(self.nf.build_title("info", "通知测试", "电信"), "🔔 电信 · 通知测试")

    def test_site_fallback(self):
        for site in ("", None, "   ", "a\nb", "x" * 21, 'say "hi"'):
            with self.subTest(site=site):
                self.assertEqual(self.nf.build_title("ok", "测试", site), "✅ mihomo 网关 · 测试")
        self.assertEqual(self.nf.build_title("ok", "测试", "  联通 "), "✅ 联通 · 测试")
        self.assertEqual(self.nf.build_title("bogus", "测试", "联通"), "🔔 联通 · 测试")

    def test_body_is_plain_lines(self):
        body = self.nf.build_body(["v1.19.27 → v1.19.32", "", "  健康检查通过：服务、DNS、入口端口  ", None])
        self.assertEqual(body, "v1.19.27 → v1.19.32\n健康检查通过：服务、DNS、入口端口")
        self.assertEqual(self.nf.build_body(["a\nb"]), "a b", "一行里不能夹换行")

    def test_site_name_validation(self):
        self.assertEqual(self.nf.validate_site_name(" 联通 "), (True, "联通", ""))
        self.assertTrue(self.nf.validate_site_name("")[0])
        self.assertTrue(self.nf.validate_site_name("一" * 20)[0])
        for bad in ("一" * 21, "a\nb", "a\rb", "it's", 'say "x"', "a\\b", "$HOME", "`id`"):
            with self.subTest(bad=bad):
                ok, _, message = self.nf.validate_site_name(bad)
                self.assertFalse(ok)
                self.assertTrue(message)


class DedupeTest(unittest.TestCase):
    def setUp(self):
        self.nf = load_format()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "notify_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, level, now, subject="订阅节点刷新失败", lines=("机场「A」返回 HTTP 503",)):
        return self.nf.prepare(level, subject, list(lines), site="熙国", key="sub_providers", state_file=self.state, now=now)

    def test_first_failure_repeat_reminder_and_recovery(self):
        first = self.send("warn", NOW)
        self.assertEqual(first, ("⚠️ 熙国 · 订阅节点刷新失败", "机场「A」返回 HTTP 503"))
        self.assertIsNone(self.send("warn", NOW + DAY), "3 天内不重复")
        self.assertIsNone(self.send("warn", NOW + 3 * DAY - 60))
        reminder = self.send("warn", NOW + 3 * DAY)
        self.assertEqual(reminder[1], "机场「A」返回 HTTP 503\n持续失败第 4 天")
        self.assertIsNone(self.send("warn", NOW + 4 * DAY), "提醒之后重新计 3 天")
        self.assertEqual(self.send("warn", NOW + 6 * DAY)[1].splitlines()[-1], "持续失败第 7 天")
        recovered = self.send("ok", NOW + 7 * DAY, subject="订阅节点刷新已恢复", lines=["2 个机场订阅的节点都已正常刷新"])
        self.assertEqual(recovered, ("✅ 熙国 · 订阅节点刷新已恢复", "2 个机场订阅的节点都已正常刷新"))
        self.assertIsNone(self.send("ok", NOW + 8 * DAY, subject="订阅节点刷新已恢复"), "本来就正常不发")
        self.assertIsNotNone(self.send("warn", NOW + 9 * DAY), "恢复后再失败算新的一次")

    def test_level_change_notifies_again(self):
        self.send("warn", NOW)
        changed = self.send("fail", NOW + DAY)
        self.assertTrue(changed[0].startswith("❌ "))
        self.assertIn("持续失败第 2 天", changed[1])

    def test_keys_are_independent_and_state_is_json(self):
        self.send("warn", NOW)
        other = self.nf.prepare("warn", "Geo 数据更新失败", ["x"], site="熙国", key="geo", state_file=self.state, now=NOW)
        self.assertIsNotNone(other)
        data = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["geo", "sub_providers"])
        self.assertEqual(data["geo"]["since"], NOW)

    def test_corrupt_state_file_does_not_block_notifications(self):
        Path(self.state).write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(self.send("warn", NOW))

    def test_no_key_never_dedupes(self):
        for _ in range(3):
            self.assertIsNotNone(self.nf.prepare("warn", "x", ["y"], site="", state_file=self.state, now=NOW))
        self.assertFalse(os.path.exists(self.state))


class NotifyShellTest(unittest.TestCase):
    """跑真的 notify.sh：假 curl 把 -d 的 JSON 记下来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mihomo = root / "mihomo"
        (self.mihomo / "scripts").mkdir(parents=True)
        (self.mihomo / "manager").mkdir()
        for name in ("notify.sh", "envutil.sh"):
            (self.mihomo / "scripts" / name).write_text(text(SCRIPTS / name), encoding="utf-8")
        (self.mihomo / "manager" / "notify_format.py").write_text(text(FORMAT), encoding="utf-8")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.captured = root / "captured.jsonl"
        curl = self.bin / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-d\" ]; then printf '%s\\n' \"$2\" >> \"$CAPTURE\"; shift; fi\n"
            "  shift\n"
            "done\n"
            "printf 200\n", encoding="utf-8")
        curl.chmod(0o755)
        self.state = root / "notify_state.json"
        self.write_env("联通")

    def tearDown(self):
        self.tmp.cleanup()

    def write_env(self, site):
        lines = ["NOTIFY_API=true", "NOTIFY_API_URL='http://hook.example/x'"]
        if site is not None:
            lines.append(f"SITE_NAME='{site}'")
        (self.mihomo / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_notify(self, *args, now=NOW):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", MIHOMO_DIR=str(self.mihomo),
                   CAPTURE=str(self.captured), NOTIFY_LOG_FILE=str(self.mihomo / "notify.log"),
                   NOTIFY_STATE_FILE=str(self.state), NOTIFY_NOW=str(now))
        return subprocess.run(["bash", str(self.mihomo / "scripts" / "notify.sh"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def messages(self):
        if not self.captured.exists():
            return []
        return [json.loads(line) for line in self.captured.read_text(encoding="utf-8").splitlines()]

    def split(self, message):
        body, _, stamp = message["content"].rpartition("\n\n")
        self.assertRegex(stamp, r"^📅 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        return message["title"], body

    def test_event_form_builds_title_and_body(self):
        out = self.run_notify("--event", "ok", "mihomo 内核已更新", "v1.19.27 → v1.19.32", "健康检查通过：服务、DNS、入口端口")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Webhook: 已发送", out.stdout)
        [message] = self.messages()
        self.assertEqual(self.split(message), ("✅ 联通 · mihomo 内核已更新", "v1.19.27 → v1.19.32\n健康检查通过：服务、DNS、入口端口"))

    def test_failure_example_and_missing_site_name(self):
        self.write_env(None)
        self.run_notify("--event", "warn", "订阅节点刷新失败", "机场「我的机场」返回 HTTP 503，可能是订阅链接失效", "配置未变化，代理继续使用旧节点")
        [message] = self.messages()
        self.assertEqual(self.split(message), ("⚠️ mihomo 网关 · 订阅节点刷新失败",
                                               "机场「我的机场」返回 HTTP 503，可能是订阅链接失效\n配置未变化，代理继续使用旧节点"))

    def test_old_two_argument_form_still_works(self):
        out = self.run_notify("旧标题", "旧正文\n第二行")
        self.assertEqual(out.returncode, 0, out.stderr)
        [message] = self.messages()
        self.assertEqual(self.split(message), ("旧标题", "旧正文\n第二行"))

    def test_info_test_message(self):
        self.run_notify("--event", "info", "通知测试", "通知通道正常")
        self.assertEqual(self.split(self.messages()[0]), ("🔔 联通 · 通知测试", "通知通道正常"))

    def test_key_dedupes_across_runs(self):
        args = ("--event", "warn", "--key", "geo", "Geo 数据更新失败", "都没下载成功")
        self.run_notify(*args, now=NOW)
        skipped = self.run_notify(*args, now=NOW + DAY)
        self.assertIn("不重复发送", skipped.stdout)
        self.run_notify(*args, now=NOW + 3 * DAY)
        self.run_notify("--event", "ok", "--key", "geo", "Geo 数据更新已恢复", "三个 Geo 数据文件都已下载成功", now=NOW + 4 * DAY)
        self.run_notify("--event", "ok", "--key", "geo", "Geo 数据更新已恢复", "三个 Geo 数据文件都已下载成功", now=NOW + 5 * DAY)
        titles = [self.split(m) for m in self.messages()]
        self.assertEqual([t for t, _ in titles], ["⚠️ 联通 · Geo 数据更新失败", "⚠️ 联通 · Geo 数据更新失败", "✅ 联通 · Geo 数据更新已恢复"])
        self.assertEqual(titles[1][1], "都没下载成功\n持续失败第 4 天")

    def test_bad_level_is_rejected(self):
        out = self.run_notify("--event", "boom", "x")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(self.messages(), [])

    def test_site_name_with_shell_syntax_is_not_executed(self):
        (self.mihomo / ".env").write_text("NOTIFY_API=true\nNOTIFY_API_URL='http://h/x'\nSITE_NAME='a\"b'\n", encoding="utf-8")
        self.run_notify("--event", "info", "通知测试", "通知通道正常")
        self.assertEqual(self.messages()[0]["title"], "🔔 mihomo 网关 · 通知测试", "非法站点名退回默认")


class PanelNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = load_app(self.tmp.name)
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_notify_event_reads_site_name_from_env(self):
        result = self.app.notify_event("info", "通知测试", ["通知通道正常"], send=lambda t, c: self.sent.append((t, c)))
        self.assertEqual(self.sent, [("🔔 mihomo 网关 · 通知测试", "通知通道正常")])
        self.app.write_env({"SITE_NAME": "电信"})
        self.app.notify_event("ok", "管理面板已更新", ["v0.1.36 → v0.1.37"], send=lambda t, c: self.sent.append((t, c)))
        self.assertEqual(self.sent[-1][0], "✅ 电信 · 管理面板已更新")
        self.assertEqual(result, ("🔔 mihomo 网关 · 通知测试", "通知通道正常"))

    def test_notify_event_key_uses_state_file_under_mihomo_dir(self):
        send = lambda t, c: self.sent.append((t, c))
        self.app.notify_event("warn", "x", ["y"], key="k", send=send, now=NOW)
        self.assertIsNone(self.app.notify_event("warn", "x", ["y"], key="k", send=send, now=NOW + 60))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue((self.dir / "notify_state.json").exists())

    def finalize(self, panel_version, now):
        state = self.app.read_auto_update_state(str(self.dir / "state.json"))
        state["items"]["panel"] = {"last_result": "started", "from": "0.1.36", "to": "0.1.37", "started_at": NOW}
        self.app.write_auto_update_state(state, str(self.dir / "state.json"))
        self.app.PANEL_VERSION = panel_version
        self.app.AUTO_UPDATE_LOG = str(self.dir / "auto.log")
        return self.app.finalize_panel_auto_update(str(self.dir / "state.json"), now=now,
                                                   notify=lambda t, c: self.sent.append((t, c)))

    def test_panel_upgrade_finalize_messages(self):
        self.assertEqual(self.finalize("0.1.37", NOW + 30), "updated")
        self.assertEqual(self.sent[-1], ("✅ mihomo 网关 · 管理面板已更新", "v0.1.36 → v0.1.37\n新版本已启动，面板可以正常访问"))
        self.assertEqual(self.finalize("0.1.36", NOW + 3600), "failed")
        self.assertEqual(self.sent[-1], ("❌ mihomo 网关 · 管理面板更新失败，已回滚",
                                         "v0.1.36 → v0.1.37\n升级 10 分钟后仍在运行 v0.1.36，新版本没有启动成功\n当前继续使用 v0.1.36"))

    def test_test_notify_button_uses_event_form(self):
        self.assertIn("'test_notify': ['bash', f'{SCRIPT_DIR}/notify.sh', '--event', 'info', '通知测试', '通知通道正常']", text(APP))


class AutoUpdateNoticeTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("auto_update_notice_under_test", AUTO_UPDATE)
        self.au = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.au)

    def test_core_updated(self):
        report = self.au.make_result("core", "updated", "x", from_version="v1.19.27", to_version="v1.19.32",
                                     detail_lines=[f"健康检查通过：{self.au.health_summary({'dns': ('127.0.0.1', 53), 'ports': {7890}})}"])
        self.assertEqual(self.au.notice_for(report), ("ok", "mihomo 内核已更新", ["v1.19.27 → v1.19.32", "健康检查通过：服务、DNS、入口端口"]))
        self.assertEqual(self.au.health_summary({"dns": None, "ports": set()}), "服务")

    def test_core_failed_without_changes(self):
        report = self.au.make_result("core", "failed", "新内核 v1.19.32 校验当前配置失败，未做改动：\nyaml: line 3", "v1.19.27", "v1.19.32",
                                     "v1.19.27", "v1.19.32", reason="新版本不认当前配置（v1.19.32 校验 config.yaml 未通过）")
        level, subject, lines = self.au.notice_for(report)
        self.assertEqual((level, subject), ("warn", "mihomo 内核更新失败"))
        self.assertEqual(lines, ["v1.19.27 → v1.19.32", "新版本不认当前配置（v1.19.32 校验 config.yaml 未通过）", "未做任何改动，继续运行 v1.19.27"])

    def test_ui_and_panel_names(self):
        ui = self.au.make_result("ui", "rolled_back", "x", reason="新版本下载后页面打不开", detail_lines=["已恢复原来的 Dashboard，可以继续使用"])
        self.assertEqual(self.au.notice_for(ui), ("fail", "Dashboard 更新失败，已回滚", ["新版本下载后页面打不开", "已恢复原来的 Dashboard，可以继续使用"]))
        ok = self.au.make_result("ui", "updated", "x", detail_lines=["已重新下载最新版本"])
        self.assertEqual(self.au.notice_for(ok)[:2], ("ok", "Dashboard 已更新"))
        panel = self.au.make_result("panel", "failed", "面板升级失败，已自动回滚：boom", from_version="0.1.36", to_version="0.1.37",
                                    reason="升级没有完成：面板升级失败，已自动回滚：boom", detail_lines=["继续运行 v0.1.36"])
        self.assertEqual(self.au.notice_for(panel), ("warn", "管理面板更新失败",
                                                     ["v0.1.36 → v0.1.37", "升级没有完成：面板升级失败，已自动回滚：boom", "继续运行 v0.1.36"]))

    def test_quiet_results_do_not_notify(self):
        for result in ("up_to_date", "skipped", "not_due", "available", "started"):
            self.assertIsNone(self.au.notice_for(self.au.make_result("core", result, "x")))

    def test_every_notice_fits_four_lines(self):
        rollback = self.au.make_result("core", "rolled_back", "x", from_version="v1", to_version="v2", reason="r",
                                       detail_lines=["已换回 v1，但仍不正常：服务没有运行", "需要尽快人工处理"])
        self.assertLessEqual(len(self.au.notice_for(rollback)[2]), 4)


class CallSiteContractTest(unittest.TestCase):
    def test_shell_callers_use_event_form(self):
        sub = text(SCRIPTS / "update_subscription.sh")
        geo = text(SCRIPTS / "update_geo.sh")
        for script in (sub, geo):
            self.assertIn('bash "$NOTIFY_SCRIPT" --event "$@"', script)
            self.assertIsNone(re.search(r'bash "\$NOTIFY_SCRIPT" "[^-]', script), "不再用旧的两参数形式")
        for subject in ("订阅配置下载失败", "订阅配置生成失败", "订阅配置无效", "订阅配置应用失败，已回滚", "订阅配置已更新"):
            self.assertIn(f'"{subject}"', sub)
        self.assertIn('notify_event warn --key sub_providers "订阅节点刷新失败"', sub)
        self.assertIn('notify_event ok --key sub_providers "订阅节点刷新已恢复"', sub)
        self.assertIn("订阅链接可能已失效或机场故障", sub)
        self.assertIn('notify_event warn --key geo "Geo 数据更新失败"', geo)
        self.assertIn('notify_event warn --key geo "Geo 数据部分更新失败"', geo)
        self.assertIn('notify_event ok --key geo "Geo 数据更新已恢复"', geo)
        self.assertIn('notify.sh" --event info "通知测试" "通知通道正常"', text(CLI))

    def test_no_internal_codes_or_markdown_in_messages(self):
        for path in (SCRIPTS / "update_subscription.sh", SCRIPTS / "update_geo.sh"):
            for line in text(path).splitlines():
                if "notify_event " in line and not line.strip().startswith(("notify_event()", "#")):
                    with self.subTest(line=line.strip()):
                        self.assertNotIn("exit=", line)
                        self.assertNotIn("**", line)
                        self.assertNotRegex(line, r'"(- |> )')

    def test_python_callers_use_notify_event(self):
        app = text(APP)
        au = text(AUTO_UPDATE)
        self.assertIn("def notify_event(level, subject, lines, key=None, send=None", app)
        self.assertIn("notify_event(*notice, send=notify)", app)
        self.assertIn("rt.panel.notify_event(*notice, send=rt.notify)", au)
        self.assertNotIn("gethostname()}", app + au, "标题不再用容器主机名")
        self.assertNotIn("Mihomo 自动更新（{", app + au)
        self.assertIn("import notify_format", app)

    def test_every_notify_sh_invocation_uses_event(self):
        # 脚本：所有 bash "$NOTIFY_SCRIPT" 调用都带 --event
        for path in SCRIPTS.glob("*.sh"):
            for line in text(path).splitlines():
                if 'bash "$NOTIFY_SCRIPT"' in line:
                    with self.subTest(file=path.name, line=line.strip()):
                        self.assertIn("--event", line)
        # CLI 和面板：除了 run_notify 的投递（标题正文由 notify_event 生成），都带 --event
        for path in (APP, CLI):
            for line in text(path).splitlines():
                if "notify.sh" not in line or "bash" not in line or line.strip().startswith("#"):
                    continue
                if 'notify.sh", title, content]' in line:
                    continue
                with self.subTest(file=path.name, line=line.strip()):
                    self.assertIn("--event", line)


class SiteNameSettingTest(unittest.TestCase):
    def test_settings_api_reads_validates_and_writes_site_name(self):
        app = text(APP)
        self.assertIn('"site_name": e.get(\'SITE_NAME\', \'\')', app)
        self.assertIn("notify_format.validate_site_name(d.get('site_name'))", app)
        self.assertIn('updates["SITE_NAME"] = site_name', app)
        self.assertIn("if 'site_name' in d:", app, "没带站点名的保存不能把它清空")

    def test_notify_page_has_site_name_field(self):
        html = text(INDEX)
        notify_tab = html[html.index('id="tab-notify"'):]
        notify_tab = notify_tab[:notify_tab.index("</section>")]
        self.assertIn('id="site_name"', notify_tab)
        self.assertIn('maxlength="20"', notify_tab)
        self.assertIn("站点名称", notify_tab)
        self.assertIn("payload.site_name = siteName", html)
        self.assertIn("siteNameField.dataset.loaded = '1'", html)


if __name__ == "__main__":
    unittest.main()
