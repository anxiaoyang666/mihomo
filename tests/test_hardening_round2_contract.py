"""第二轮加固的契约测试（grep 源码）。

覆盖：控制器密钥、内核安装、路径/变量名统一、卸载、.env 权限、TLS、日志轮转、
/api/rule-sync、并发锁、Web 鉴权、前端 api() 和 notify/cron/gateway 小修。
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "remote-root"
APP = REMOTE / "etc" / "mihomo" / "manager" / "app.py"
INDEX = REMOTE / "etc" / "mihomo" / "manager" / "templates" / "index.html"
LOGIN = REMOTE / "etc" / "mihomo" / "manager" / "templates" / "login.html"
SCRIPTS = REMOTE / "etc" / "mihomo" / "scripts"
CLI = REMOTE / "usr" / "bin" / "mihomo"
INSTALL = ROOT / "install.sh"
LOGROTATE = REMOTE / "etc" / "logrotate.d" / "mihomo"
TEMPLATE = REMOTE / "etc" / "mihomo" / "templates" / "default.yaml"
EXAMPLE = REMOTE / "etc" / "mihomo" / "config.example.yaml"


def text(path):
    return path.read_text(encoding="utf-8")


class ControllerSecretContractTest(unittest.TestCase):
    def test_templates_keep_lan_bind_but_document_generated_secret(self):
        for path in (TEMPLATE, EXAMPLE):
            source = text(path)
            with self.subTest(path=path.name):
                self.assertIn("external-controller: 0.0.0.0:9090", source)
                self.assertIn("install.sh 会生成随机密钥", source)

    def test_installer_generates_and_syncs_api_secret(self):
        install = text(INSTALL)

        self.assertIn('write_env_line "MIHOMO_API_SECRET" "$api_secret"', install)
        self.assertIn("sync_controller_secret()", install)
        self.assertIn("sync_controller_secret\n", install)
        self.assertIn('env_upsert "MIHOMO_API_SECRET"', install)
        self.assertIn("不能把它绑回 127.0.0.1", install)

    def test_subscription_update_carries_secret_into_new_config(self):
        source = text(SCRIPTS / "update_subscription.sh")

        self.assertIn("export MIHOMO_API_SECRET", source)
        self.assertIn("config['secret'] = secret", source)
        self.assertIn("已保留控制器密钥", source)


class KernelInstallContractTest(unittest.TestCase):
    def test_install_kernel_fails_loudly_and_never_downgrades(self):
        source = text(SCRIPTS / "install_kernel.sh")

        self.assertIn('CORE_BIN="/usr/bin/mihomo-core"', source)
        self.assertIn('MIHOMO_PATH="${MIHOMO_PATH:-/etc/mihomo}"', source)
        self.assertIn("curl -fL --max-time", source)
        self.assertIn("curl -fsSL --max-time 20", source)
        self.assertIn("无法获取 mihomo 最新版本号", source)
        self.assertIn("拒绝降级", source)
        self.assertIn("sort -V", source)
        self.assertIn('gzip -t "$GZ_FILE" || fail', source)
        self.assertIn('gunzip -f "$GZ_FILE" || fail', source)
        self.assertIn("没有 sha256 校验文件", source)
        self.assertIn('releases/latest/download/version.txt', source)
        # 直连在前，GH_PROXY 在后
        self.assertIn('echo "$url"\n    if [ -n "$GH_PROXY" ]; then\n        echo "${GH_PROXY%/}/${url}"', source)
        self.assertNotIn("v1.18.1", source)
        self.assertNotIn("grep -oP", source)

    def test_installer_treats_kernel_failure_as_warning(self):
        install = text(INSTALL)

        self.assertIn('if ! bash "$INSTALL_DIR/scripts/install_kernel.sh" auto; then', install)
        self.assertIn("警告：内核安装失败，面板和脚本已安装", install)
        self.assertNotIn('install -m 0755 "$INSTALL_DIR/mihomo" /usr/bin/mihomo-core', install)


class SplitBrainContractTest(unittest.TestCase):
    def test_core_binary_path_is_single_source_of_truth(self):
        for name in ("install_kernel.sh", "update_subscription.sh"):
            source = text(SCRIPTS / name)
            with self.subTest(script=name):
                self.assertIn('CORE_BIN="/usr/bin/mihomo-core"', source)
                self.assertNotIn("${MIHOMO_PATH}/mihomo -t", source)
                self.assertNotIn("ExecStart=${MIHOMO_PATH}/mihomo", source)
        self.assertIn('CORE_BIN="/usr/bin/mihomo-core"', text(CLI))

    def test_cli_and_scripts_write_keys_the_update_script_reads(self):
        cli = text(CLI)
        self.assertIn('upsert_env "CONFIG_MODE" "airport"', cli)
        self.assertIn('upsert_env "SUB_URL_AIRPORT" "$url"', cli)
        self.assertIn('upsert_env "CONFIG_MODE" "raw"', cli)
        self.assertIn('upsert_env "SUB_URL_RAW" "$url"', cli)
        self.assertNotIn('upsert_env "SUB_URL" ', cli)

        update = text(SCRIPTS / "update_subscription.sh")
        self.assertIn('"$CONFIG_MODE" == "raw"', update)
        self.assertIn("SUB_URL_RAW", update)
        self.assertIn("SUB_URL_AIRPORT", update)

    def test_notify_env_names_match(self):
        app = text(APP)
        notify = text(SCRIPTS / "notify.sh")

        self.assertIn('"NOTIFY_API_URL"', app)
        self.assertIn('-n "$NOTIFY_API_URL"', notify)


class UninstallContractTest(unittest.TestCase):
    def test_uninstall_script_reverses_installer_and_gateway_init(self):
        source = text(SCRIPTS / "uninstall.sh")

        self.assertIn("systemctl stop mihomo mihomo-manager force-ip-forward", source)
        self.assertIn("rm -f /etc/systemd/system/force-ip-forward.service", source)
        self.assertIn("rm -f /etc/sysctl.d/99-mihomo-gateway.conf", source)
        self.assertIn("rm -f /etc/logrotate.d/mihomo", source)
        self.assertIn("rm -f /usr/bin/mihomo /usr/bin/mihomo-core", source)
        for marker in ("gateway_init.sh", "MIHOMO_AUTOMATION", "# JOB_SUB", "# JOB_GEO", "/etc/mihomo/scripts/"):
            self.assertIn(f'grep -F -v -- "{marker}"', source)
        self.assertNotIn("mihomo-cli", source)
        self.assertNotIn("mihomo-tools", source)

    def test_cli_uninstall_delegates_with_confirmation(self):
        cli = text(CLI)

        self.assertIn("uninstall_toolbox()", cli)
        self.assertIn('read -p "确认卸载? (y/n): " ack', cli)
        self.assertIn('bash "${SCRIPT_DIR}/uninstall.sh" -y "$data_flag"', cli)
        self.assertNotIn("mihomo-tools", cli)
        self.assertNotIn("mihomo-cli", cli)


class EnvFileContractTest(unittest.TestCase):
    def test_backend_env_writes_are_atomic_and_private(self):
        app = text(APP)

        self.assertIn("os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)", app)
        self.assertIn("os.replace(tmp_path, ENV_FILE)", app)
        self.assertIn("def rotate_session_secret", app)
        self.assertNotIn("env.get('WEB_USER', 'admin')", app)
        self.assertNotIn("env.get('WEB_SECRET', 'admin')", app)
        self.assertIn("缺少 WEB_USER / WEB_SECRET", app)

    def test_shell_upsert_env_sets_0600(self):
        source = text(SCRIPTS / "envutil.sh")
        self.assertIn("os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600", source)
        self.assertIn('chmod 600 "$ENV_FILE"', source)
        self.assertIn("os.replace(tmp, path)", source)

    def test_login_page_no_longer_advertises_default_credentials(self):
        self.assertNotIn("默认 admin", text(LOGIN))


class TlsContractTest(unittest.TestCase):
    def test_subscription_update_verifies_tls_by_default(self):
        source = text(SCRIPTS / "update_subscription.sh")

        self.assertIn('WGET_OPTS=(--timeout=30 --tries=2 --user-agent="$SUB_USER_AGENT")', source)
        self.assertIn('wget "${WGET_OPTS[@]}" -O "$TEMP_NEW" "$SUB_URL_RAW"', source)
        self.assertNotIn("wget --no-check-certificate", source)


class LogHandlingContractTest(unittest.TestCase):
    def test_log_reads_only_tail_of_file(self):
        app = text(APP)

        self.assertIn("LOG_TAIL_BYTES = 256 * 1024", app)
        self.assertIn("f.seek(0, os.SEEK_END)", app)
        self.assertIn("start = max(0, size - tail_bytes)", app)
        self.assertIn('text = read_recent_log_lines(LOG_FILE, 100000, tail_bytes=LOG_SUMMARY_TAIL_BYTES)', app)
        self.assertNotIn("f.readlines()[-200:]", app)

    def test_logrotate_snippet_is_installed_and_managed(self):
        snippet = text(LOGROTATE)
        for directive in ("daily", "rotate 7", "compress", "copytruncate", "/var/log/mihomo.log"):
            self.assertIn(directive, snippet)
        self.assertIn('install -m 0644 "$payload/etc/logrotate.d/mihomo" /etc/logrotate.d/mihomo', text(INSTALL))
        self.assertIn('("/etc/logrotate.d/mihomo", "etc/logrotate.d/mihomo", "file", 0o644)', text(APP))


class RuleSyncHardeningContractTest(unittest.TestCase):
    def test_rule_sync_endpoint_checks_token_before_parsing_and_requires_dict(self):
        app = text(APP)

        self.assertIn("MAX_CONTENT_LENGTH=1 * 1024 * 1024", app)
        self.assertIn("def sync_token_matches", app)
        self.assertIn('secrets.compare_digest(str(provided).encode("utf-8"), str(expected).encode("utf-8"))', app)
        section = app[app.find("def api_rule_sync"): app.find("def api_rules")]
        self.assertLess(section.find("header_token and not sync_token_matches"), section.find("request.get_json(silent=True)"))
        self.assertIn("if not isinstance(data, dict):", section)
        self.assertIn('"请求体必须是 JSON 对象"}), 400', section)

    def test_other_routes_use_json_body_helper(self):
        app = text(APP)

        self.assertIn("def json_body", app)
        self.assertIn("return data if isinstance(data, dict) else {}", app)
        self.assertNotIn("request.json", app)


class ConcurrencyContractTest(unittest.TestCase):
    def test_config_mutation_is_serialised(self):
        app = text(APP)

        self.assertIn("CONFIG_LOCK = threading.RLock()", app)
        self.assertIn('BUSY_MESSAGE = "另一个操作正在进行中，请稍后再试。"', app)
        self.assertGreaterEqual(app.count("CONFIG_LOCK.acquire(blocking=False)"), 5)
        self.assertIn("tempfile.NamedTemporaryFile(", app)
        self.assertNotIn('f"{CONFIG_FILE}.rulesync"', app)
        self.assertNotIn('f"{CONFIG_FILE}.webcheck"', app)
        self.assertIn("except subprocess.TimeoutExpired:", app)


class WebAuthContractTest(unittest.TestCase):
    def test_session_cookie_flags_and_login_throttle(self):
        app = text(APP)

        self.assertIn('SESSION_COOKIE_SAMESITE="Lax"', app)
        self.assertIn("SESSION_COOKIE_HTTPONLY=True", app)
        self.assertIn("LOGIN_MAX_FAILURES = 5", app)
        self.assertIn("LOGIN_LOCKOUT_SECONDS = 60", app)
        self.assertIn("def record_login_failure", app)
        self.assertIn("登录失败次数过多", app)

    def test_state_changing_api_requires_xhr_header_and_logout_is_post(self):
        app = text(APP)
        index = text(INDEX)

        self.assertIn('request.headers.get("X-Requested-With", "") == "XMLHttpRequest"', app)
        self.assertIn('request.method not in ("GET", "HEAD", "OPTIONS") and not is_xhr_request()', app)
        self.assertIn("@app.route('/logout', methods=['POST'])", app)
        self.assertIn("'X-Requested-With': 'XMLHttpRequest'", index)
        self.assertIn('<form method="POST" action="/logout"', index)
        self.assertNotIn('<a href="/logout"', index)

    def test_password_change_rotates_session_secret(self):
        app = text(APP)
        section = app[app.find("def update_account_credentials"): app.find("def api_rule_sync_settings")]
        self.assertIn("rotate_session_secret()", section)


class FrontendContractTest(unittest.TestCase):
    def test_toast_calls_use_two_arguments(self):
        index = text(INDEX)

        self.assertNotIn("showToast('已生成随机密钥', '保存同步设置后生效。', 'info')", index)
        self.assertNotIn("showToast('没有可复制的密钥', '请先生成或填写同步密钥。', 'warning')", index)
        self.assertNotIn("showToast('已复制同步密钥', '可以粘贴到其他 mosctl / mihomo 面板。', 'success')", index)
        self.assertIn("showToast('已生成随机密钥，保存同步设置后生效。', 'info')", index)

    def test_api_helper_checks_ok_and_parses_json_inside_try(self):
        index = text(INDEX)
        section = index[index.find("async function api("): index.find("async function control(")]

        self.assertIn("body = await resp.json();", section)
        self.assertIn("if (!resp.ok) {", section)
        self.assertNotIn("return resp.json();", section)
        self.assertIn("return { success: false, message: '登录已失效，请重新登录。' };", section)

    def test_load_logs_guards_missing_response(self):
        index = text(INDEX)
        section = index[index.find("function loadLogs("): index.find("function logLevelOf(")]
        self.assertIn("if (!res || typeof res.logs !== 'string') return;", section)


class SmallFixesContractTest(unittest.TestCase):
    def test_notify_json_escaping_and_token_handling(self):
        notify = text(SCRIPTS / "notify.sh")

        self.assertIn("json_escape()", notify)
        self.assertIn("json.dumps(sys.argv[1]", notify)
        # sed 回退：先转义反斜杠再转义引号
        self.assertIn("-e 's/\\\\/\\\\\\\\/g' -e 's/\"/\\\\\"/g'", notify)
        self.assertIn("curl -sS -m 20 -K -", notify)
        self.assertNotIn('-X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}', notify)
        self.assertIn('2>"${TMP_DIR}/tg.err"', notify)

    def test_python_snippets_take_paths_via_argv(self):
        update = text(SCRIPTS / "update_subscription.sh")
        geo = text(SCRIPTS / "update_geo.sh")

        self.assertIn('python3 - "$CONFIG_FILE" <<\'PY\'', geo)
        self.assertIn("open(sys.argv[1]", geo)
        self.assertIn('python3 - "$TEMPLATE_FILE" "$TEMP_NEW" <<\'PY\'', update)
        self.assertIn('python3 - "$TEMP_NEW" "${MIHOMO_DIR}/manager" <<\'PY\'', update)
        self.assertNotIn("template_path = '$TEMPLATE_FILE'", update)
        self.assertNotIn("config_path = '$TEMP_NEW'", update)
        self.assertNotIn('python3 -c "', update)

    def test_update_cron_reports_failures(self):
        app = text(APP)
        section = app[app.find("def update_cron"): app.find("def parse_daily_time")]

        self.assertIn("if result.returncode != 0:", section)
        self.assertIn("return False, f\"写入定时任务失败", section)
        self.assertNotIn("except: pass", section)
        self.assertIn("定时任务更新失败", app)

    def test_gateway_init_checks_forward_policy(self):
        gateway = text(SCRIPTS / "gateway_init.sh")

        self.assertIn("iptables -S FORWARD 2>/dev/null | grep -q '^-P FORWARD ACCEPT'", gateway)
        self.assertNotIn("iptables -C FORWARD -j ACCEPT", gateway)

    def test_backups_are_pruned_to_keep_count(self):
        app = text(APP)
        update = text(SCRIPTS / "update_subscription.sh")

        self.assertIn("def prune_config_backups", app)
        self.assertIn('read_env().get("BACKUP_KEEP_COUNT", DEFAULT_BACKUP_KEEP_COUNT)', app)
        self.assertIn("DEFAULT_BACKUP_KEEP_COUNT = 10", app)
        self.assertIn('BACKUP_KEEP_COUNT="${BACKUP_KEEP_COUNT:-10}"', update)
        self.assertIn("prune_backups()", update)
        self.assertIn("prune_backups\n", update)


if __name__ == "__main__":
    unittest.main()
