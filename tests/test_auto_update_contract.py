"""自动更新的接线契约：面板卡片、路由、启动钩子、卸载清理、日志轮转。"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "remote-root"
MANAGER = REMOTE / "etc" / "mihomo" / "manager"
APP = MANAGER / "app.py"
AUTO_UPDATE = MANAGER / "auto_update.py"
INDEX = MANAGER / "templates" / "index.html"
UNINSTALL = REMOTE / "etc" / "mihomo" / "scripts" / "uninstall.sh"
CLI = REMOTE / "usr" / "bin" / "mihomo"
LOGROTATE = REMOTE / "etc" / "logrotate.d" / "mihomo"


def text(path):
    return path.read_text(encoding="utf-8")


def route_block(source, route):
    start = source.index(route)
    end = source.index("@app.route", start + len(route))
    return source[start:end]


class AutoUpdateUiContractTest(unittest.TestCase):
    def test_card_lives_on_tasks_page_with_all_controls(self):
        html = text(INDEX)
        tasks = html[html.index('id="tab-tasks"'):html.index('id="tab-rules"')]
        self.assertIn('id="autoUpdateCard"', tasks)
        self.assertIn("自动更新", tasks)
        for element_id in ("auto_update_enabled", "auto_update_time", "auto_update_tz_hint", "auto_update_core_min_age",
                           "auto_update_panel_min_age", "auto_update_ui_interval", "auto_update_items", "auto_update_report"):
            self.assertIn(f'id="{element_id}"', tasks)
        self.assertIn("立即检查（不安装）", tasks)
        self.assertIn("立即更新", tasks)
        self.assertIn('min="0" max="90"', tasks)
        self.assertIn('min="1" max="365"', tasks)

    def test_card_shows_server_timezone_and_browser_local_time(self):
        html = text(INDEX)
        self.assertIn("function serverTimeToBrowserLocal", html)
        self.assertIn("服务器时区", html)
        self.assertIn("相当于你浏览器本地时间", html)

    def test_run_button_confirms_restart_and_polls(self):
        html = text(INDEX)
        body = html[html.index("async function runAutoUpdate"):]
        body = body[:body.index("\n    }\n") + 6]
        self.assertIn("askConfirm(", body)
        self.assertIn("重启 mihomo", body)
        self.assertIn("1–2 秒", body)
        self.assertIn("api('/auto-update/run'", body)
        self.assertIn("pollAutoUpdate(", body)
        self.assertIn("api('/auto-update/check'", html)
        self.assertIn("api('/auto-update/settings'", html)
        self.assertIn("loadAutoUpdate();", html[html.index("function loadConfig()"):html.index("function loadConfig()") + 300])


class AutoUpdateBackendContractTest(unittest.TestCase):
    def test_routes_require_login_and_lock(self):
        source = text(APP)
        for route in ("@app.route('/api/auto-update')", "@app.route('/api/auto-update/settings', methods=['POST'])",
                      "@app.route('/api/auto-update/check', methods=['POST'])", "@app.route('/api/auto-update/run', methods=['POST'])"):
            block = route_block(source, route)
            self.assertIn("@login_required", block.splitlines()[1], route)
        run = route_block(source, "@app.route('/api/auto-update/run', methods=['POST'])")
        self.assertIn("CONFIG_LOCK.acquire(blocking=False)", run)
        self.assertIn("auto_update_lock_busy()", run)
        self.assertIn("spawn_auto_update()", run)
        settings = route_block(source, "@app.route('/api/auto-update/settings', methods=['POST'])")
        self.assertIn("validate_auto_update_settings(", settings)
        self.assertIn("apply_auto_update_cron(", settings)

    def test_background_run_escapes_manager_cgroup(self):
        source = text(APP)
        spawn = source[source.index("def spawn_auto_update"):source.index("def json_body")]
        self.assertIn("systemd-run", spawn)
        self.assertIn("start_new_session=True", spawn)

    def test_startup_and_settings_save_ensure_cron(self):
        source = text(APP)
        main = source[source.index("if __name__ == '__main__':"):]
        self.assertIn("startup_auto_update_hooks()", main)
        self.assertLess(main.index("startup_auto_update_hooks()"), main.index("start_device_sampler()"))
        settings = source[source.index("def handle_settings"):source.index("if __name__ == '__main__':")]
        self.assertIn("ensure_auto_update_cron()", settings)
        self.assertIn('AUTO_UPDATE_JOB_ID = "# MIHOMO_AUTO_UPDATE"', source)

    def test_entry_point_imports_app_without_starting_services(self):
        source = text(AUTO_UPDATE)
        self.assertIn("sys.path.insert(0, HERE)", source)
        self.assertIn("import app as panel_module", source)
        self.assertNotIn("start_device_sampler", source)
        self.assertNotIn("app.run(", source)
        self.assertIn('"--dry-run"', source)
        self.assertIn('choices=("core", "ui", "panel")', source)
        # 顺序：UI → 内核 → 面板
        self.assertIn('for item in ("ui", "core", "panel"):', source)
        # 新内核要先自检和校验配置，再动 /usr/bin/mihomo-core
        update = source[source.index("def update_core"):source.index("def rollback_core")]
        self.assertLess(update.index('"-t", "-d"'), update.index("replace_core(rt, new_bin)"))
        self.assertLess(update.index("binary_version(rt, new_bin)"), update.index('"-t", "-d"'))

    def test_install_kernel_still_works_standalone(self):
        script = text(REMOTE / "etc" / "mihomo" / "scripts" / "install_kernel.sh")
        self.assertIn('PLATFORM="linux-amd64-compatible"', script)
        self.assertIn("gzip -t", script)
        self.assertIn("拒绝降级", script)
        self.assertIn("auto_update.py", script)


class AutoUpdateCleanupContractTest(unittest.TestCase):
    def test_uninstall_script_removes_cron_line_and_log(self):
        source = text(UNINSTALL)
        self.assertIn('grep -F -v -- "MIHOMO_AUTO_UPDATE"', source)
        self.assertIn("/var/log/mihomo-auto-update.log", source)

    def test_cli_fallback_uninstall_removes_cron_line(self):
        source = text(CLI)
        fallback = source[source.index("uninstall_toolbox() {"):source.index("show_menu() {")]
        self.assertIn('grep -F -v -- "MIHOMO_AUTO_UPDATE"', fallback)
        self.assertIn("/var/log/mihomo-auto-update.log", fallback)

    def test_logrotate_covers_auto_update_log(self):
        self.assertRegex(text(LOGROTATE), r"(?m)^/var/log/mihomo\.log .*?/var/log/mihomo-auto-update\.log \{")


if __name__ == "__main__":
    unittest.main()
