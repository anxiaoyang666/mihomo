from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "remote-root"
SCRIPTS = REMOTE / "etc" / "mihomo" / "scripts"
# 脚本的"活"引用来源：安装器、CLI、面板后端/前端、systemd 单元、README。测试和历史计划文档不算。
LIVE_REFERRERS = [
    ROOT / "install.sh",
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    REMOTE / "usr" / "bin" / "mihomo",
    REMOTE / "etc" / "mihomo" / "manager" / "app.py",
    REMOTE / "etc" / "mihomo" / "manager" / "templates" / "index.html",
    *sorted((REMOTE / "etc" / "systemd" / "system").glob("*.service")),
]
# 第三轮评审删掉的、谁都不会调用的脚本，不能再长回来
REMOVED_SCRIPTS = (
    "apply_settings.sh", "patch_config.sh", "cron_manager.sh", "watchdog.sh", "manage_config.sh",
    "manage_ui.sh", "service_ctl.sh", "set_notify.sh", "view_log.sh",
)


def source(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


def strip_comments(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class MihomoScriptStabilityContractTest(unittest.TestCase):
    def test_geo_update_downloads_to_private_temp_dir_before_replace(self):
        text = source("update_geo.sh")

        self.assertIn('TMP_DIR="$(mktemp -d)"', text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn("GEO_FILES=(geoip.dat geosite.dat geoip.metadb)", text)
        # TLS 校验默认开启，证书跳过只能通过 ALLOW_INSECURE_TLS=true 显式打开
        self.assertIn('WGET_OPTS=(--timeout=20 --tries=2)', text)
        self.assertIn('if [ "$ALLOW_INSECURE_TLS" == "true" ]; then', text)
        self.assertIn('wget "${WGET_OPTS[@]}" -O "$tmp_file"', text)
        self.assertNotIn('wget --no-check-certificate', text)
        # 内容没变不替换；没替换任何文件不重启；全部失败要通知并以非 0 退出
        self.assertIn('cmp -s "$tmp_file" "$target"', text)
        self.assertIn('mv -f "$tmp_file" "$target"', text)
        self.assertIn('if [ "$replaced" -gt 0 ]; then', text)
        self.assertIn('if [ "$failed" -eq "${#GEO_FILES[@]}" ]; then', text)
        self.assertIn('notify_event warn --key geo "Geo 数据更新失败"', text)
        self.assertIn("exit 1", text)
        self.assertNotIn(".dat.new", text)
        # 模板的规则全走 rule-providers，由控制器 API 触发内核重新拉取，不靠重启
        self.assertIn("/providers/rules/", text)
        self.assertIn("systemctl is-active --quiet mihomo", text)
        self.assertNotIn("import yaml", text)
        # 不能无条件重启：只允许在 replaced > 0 的分支里出现
        body = strip_comments(text)
        restart_at = body.find("systemctl restart mihomo")
        self.assertGreater(restart_at, body.find('if [ "$replaced" -gt 0 ]; then'))

    def test_gateway_init_writes_crontab_through_temp_file(self):
        text = source("gateway_init.sh")

        self.assertIn('TMP_DIR="$(mktemp -d)"', text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn('TMP_CRON="${TMP_DIR}/gateway_crontab"', text)
        self.assertIn('grep -F -v -- "gateway_init.sh check"', text)
        self.assertIn('crontab "$TMP_CRON"', text)
        self.assertNotIn('(crontab -l 2>/dev/null; echo', text)

    def test_gateway_init_check_is_idempotent_and_has_opt_out(self):
        text = source("gateway_init.sh")

        # 退出开关必须在 mktemp / ip route 之前，这样 check 真的什么都不碰
        opt_out = text.find('[ "${GATEWAY_AUTOFIX:-true}" == "false" ]')
        self.assertGreater(opt_out, 0)
        self.assertLess(opt_out, text.find('TMP_DIR="$(mktemp -d)"'))
        self.assertLess(opt_out, text.find("ip route show default"))
        # 每一项都先读再写
        self.assertIn('if [ "$ip_fwd" != "1" ]; then', text)
        self.assertIn("ensure_sysctl_file()", text)
        self.assertIn('[ "$(cat "$SYSCTL_FILE" 2>/dev/null)" != "$desired" ]', text)
        self.assertIn("iptables -S FORWARD 2>/dev/null | grep -q '^-P FORWARD ACCEPT'", text)
        self.assertIn('iptables -t nat -C POSTROUTING -o "$IFACE" -j MASQUERADE', text)
        self.assertIn('if [ "$(cat "$i" 2>/dev/null)" != "0" ]; then', text)
        self.assertIn("net.ipv4.conf.all.rp_filter=0", text)

    def test_every_shipped_script_is_reachable(self):
        """scripts/ 下的每个脚本都要被安装器、CLI、面板、systemd、README 或另一个活脚本引用。"""
        names = sorted(p.name for p in SCRIPTS.glob("*.sh"))
        for removed in REMOVED_SCRIPTS:
            self.assertNotIn(removed, names, f"{removed} 已被判定为不可达并删除")

        live_text = "\n".join(strip_comments(p.read_text(encoding="utf-8")) for p in LIVE_REFERRERS)
        reachable = {name for name in names if name in live_text}
        # 从活脚本出发继续传播（比如 update_subscription.sh -> notify.sh）
        changed = True
        while changed:
            changed = False
            for name in reachable.copy():
                body = strip_comments(source(name))
                for other in names:
                    if other != name and other not in reachable and other in body:
                        reachable.add(other)
                        changed = True
        self.assertEqual(sorted(reachable), names, f"不可达脚本: {sorted(set(names) - reachable)}")

    def test_envutil_is_the_single_env_writer(self):
        text = source("envutil.sh")
        self.assertIn("upsert_env()", text)
        self.assertIn("get_env()", text)
        self.assertIn('ENV_FILE="${ENV_FILE:-/etc/mihomo/.env}"', text)
        self.assertIn("comments=False", text)
        self.assertNotIn("comments=True", text)
        self.assertIn("comments=False", (ROOT / "install.sh").read_text(encoding="utf-8"))
        self.assertNotIn("comments=True", (ROOT / "install.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
