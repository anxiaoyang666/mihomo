"""自动更新卡片的显示（主题、三列表格、说人话的标签、describeAutoUpdateItem 纯函数）、
auto_update.py 记录的 latest_eligible / latest_published_at、/api/auto-update 的 ui_updated_at，
以及面板升级后旧标签页的刷新提示条。"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import unittest

from test_auto_update import AutoUpdateTestBase, DAY, NOW, iso, release

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "remote-root" / "etc" / "mihomo" / "manager"
APP = MANAGER / "app.py"
INDEX = MANAGER / "templates" / "index.html"


def index_text():
    return INDEX.read_text(encoding="utf-8")


def extract(source, *patterns):
    pieces = []
    for pattern in patterns:
        match = re.search(pattern, source)
        if match is None:
            raise AssertionError(f"没找到 {pattern}")
        pieces.append(match.group(0))
    return "\n".join(pieces)


def run_node(test, functions_js, call_js, cases):
    node = shutil.which("node")
    if not node:
        test.skipTest("node 不可用")
    script = functions_js + "\nconst cases = JSON.parse(process.argv[1]);\n" \
        f"process.stdout.write(JSON.stringify(cases.map(c => {call_js})));\n"
    out = subprocess.run([node, "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


COMPARE = r"(?ms)^    function compareAutoUpdateVersions\(.*?^    \}\n"
DESCRIBE = r"(?ms)^    function describeAutoUpdateItem\(.*?^    \}\n"
STALE = r"(?ms)^    function stalePageMessage\(.*?^    \}\n"
RESULTS = {"updated": "已更新", "up_to_date": "已是最新", "skipped": "已跳过", "failed": "失败",
           "rolled_back": "已回滚", "started": "升级已开始", "available": "有可用更新", "not_due": "未到间隔"}


class AutoUpdateCardMarkupTest(unittest.TestCase):
    def card(self):
        html = index_text()
        start = html.index('<div class="card" id="autoUpdateCard">')
        return html[start:html.index('</section>', start)]

    def test_table_uses_theme_variables_not_white_background(self):
        card = self.card()
        self.assertIn('class="update-table"', card)
        self.assertNotIn('class="table ', card)  # Bootstrap .table 自带白底
        self.assertIn("<th>项目</th><th>版本</th><th>上次检查</th>", card)
        rules = "\n".join(line for line in index_text().splitlines() if ".update-table" in line)
        self.assertIn("var(--bg-card-soft)", rules)
        self.assertIn("var(--border-color)", rules)
        self.assertIn("var(--text-muted)", rules)
        for text in (card, rules):
            self.assertNotRegex(text.lower(), r"background[^;\"]*(#fff\b|#ffffff|white|#f[0-9a-f]{5}\b)")
            self.assertNotIn("table-light", text)

    def test_labels_are_plain_language(self):
        card = self.card()
        for label in ("内核发布满几天才更新", "面板发布满几天才更新", "Dashboard 每隔几天更新",
                      "新版面板发布后等几天再更新，0 表示有新版就更新"):
            self.assertIn(label, card)
        for jargon in ("提交", "UI 间隔", "zashboard", "面板 UI", "最新可用"):
            self.assertNotIn(jargon, card)
        app = APP.read_text(encoding="utf-8")
        self.assertIn('AUTO_UPDATE_ITEM_LABELS = {"ui": "Dashboard"', app)
        labels = re.search(r"AUTO_UPDATE_NUMBER_LABELS = \{.*\}", app).group(0)
        self.assertNotIn("提交", labels)

    def test_dashboard_row_shows_update_date_from_api(self):
        html = index_text()
        self.assertIn("res.ui_updated_at ? '更新于 ' + formatDate(res.ui_updated_at)", html)


class DescribeAutoUpdateItemTest(unittest.TestCase):
    def describe(self, cases):
        js = extract(index_text(), COMPARE, DESCRIBE)
        return run_node(self, js, "describeAutoUpdateItem(c[0], c[1], c[2], c[3], c[4])",
                        [case + [RESULTS] for case in cases])

    def test_cases(self):
        now = NOW
        # 用户截图：04:00 检查时是 v0.1.36（满 2 天可装 v0.1.36），之后手动升级到 v0.1.37
        screenshot = {"current": "0.1.36", "latest": "0.1.36", "latest_eligible": "0.1.36",
                      "last_result": "up_to_date", "last_run": now - 3600, "last_check": now - 3600,
                      "message": "当前 v0.1.36，远端 v0.1.36", "from": "", "to": ""}
        core_ok = {"current": "v1.19.35", "latest": "v1.19.35", "latest_eligible": "v1.19.35",
                   "last_result": "up_to_date", "last_run": now - 60, "last_check": now - 60,
                   "message": "当前 v1.19.35，满足条件的最新稳定版 v1.19.35"}
        eligible = dict(core_ok, current="v1.19.34", last_result="available", message="可以从 v1.19.34 更新到 v1.19.35")
        waiting = dict(core_ok, latest="v1.19.36", latest_published_at=now - DAY,
                       message="当前 v1.19.35，满足条件的最新稳定版 v1.19.35；最新稳定版 v1.19.36 发布 1.0 天，不足 3 天")
        panel_waiting = dict(screenshot, current="0.1.39", latest="0.1.40", latest_eligible="", latest_published_at=now - DAY,
                             last_result="skipped", message="v0.1.40 的最新提交距今 1.0 天，不足 2 天")
        failed = dict(core_ok, current="v1.19.34", last_result="failed", message="下载 x.gz 失败：timeout\n更多",
                      **{"from": "v1.19.34", "to": "v1.19.35"})
        dry = dict(core_ok, last_dry_run={"time": now - 10, "result": "up_to_date", "message": "当前 v1.19.35"},
                   last_run=now - 5 * DAY)
        views = self.describe([
            [screenshot, "0.1.37", 2, now],
            [core_ok, "v1.19.35", 3, now],
            [eligible, "v1.19.34", 3, now],
            [waiting, "v1.19.35", 3, now],
            [dict(waiting, latest_published_at=None), "v1.19.35", 3, now],
            [panel_waiting, "0.1.39", 2, now],
            [failed, "v1.19.34", 3, now],
            [dry, "v1.19.35", 3, now],
            [{}, "", 0, now],
            [dict(waiting), "v1.19.35", 0, now],
        ])
        screen = views[0]
        self.assertEqual((screen["current"], screen["status"], screen["detail"]),
                         ("v0.1.37", "已是最新（上次检查后已手动升级）", ""))
        self.assertNotIn("v0.1.36", json.dumps(screen, ensure_ascii=False).replace("resultLabel", ""))
        self.assertEqual(views[1]["status"], "已是最新")
        self.assertEqual(views[1]["detail"], "")  # “当前…最新稳定版…” 和状态行重复
        self.assertEqual(views[1]["resultLabel"], "已是最新")
        self.assertEqual(views[1]["resultTime"], now - 60)
        self.assertEqual((views[2]["status"], views[2]["warn"]), ("可更新到 v1.19.35", True))
        self.assertEqual(views[3]["status"], "v1.19.36 已发布，满 3 天后自动更新（还差 2 天）")
        self.assertEqual(views[3]["detail"], "")
        self.assertEqual(views[4]["status"], "v1.19.36 已发布，满 3 天后自动更新")
        self.assertEqual(views[5]["status"], "v0.1.40 已发布，满 2 天后自动更新（还差 1 天）")
        self.assertEqual(views[5]["detail"], "")
        self.assertEqual(views[6]["resultLabel"], "失败 v1.19.34 → v1.19.35")
        self.assertEqual(views[6]["detail"], "下载 x.gz 失败：timeout")  # 只显示一行
        self.assertEqual(views[7]["resultLabel"], "已检查（未安装）")
        self.assertEqual(views[7]["resultTime"], now - 10)
        self.assertEqual((views[8]["current"], views[8]["status"], views[8]["resultLabel"]), ("—", "", "还没有记录"))
        self.assertEqual(views[9]["status"], "v1.19.36 已发布，下次检查时更新")


class StateRecordsReleaseInfoTest(AutoUpdateTestBase):
    def test_core_records_newest_eligible_and_published_at(self):
        self.sys.releases = [release("v1.19.36", 1), release("v1.19.35", 5)]
        self.au.run_updates(self.rt, dry_run=True, only="core")
        core = self.state()["items"]["core"]
        self.assertEqual((core["latest"], core["latest_eligible"]), ("v1.19.36", "v1.19.35"))
        self.assertEqual(core["latest_published_at"], NOW - DAY)

    def test_panel_too_new_records_commit_time_and_no_eligible(self):
        self.app.remote_panel_version = lambda settings=None: {"success": True, "latest_version": "9.9.9"}
        self.sys.commits = [{"commit": {"committer": {"date": iso(NOW - DAY)}}}]
        report = self.au.check_panel(self.rt, self.settings(panel_min_age_days=2))
        self.assertEqual(report["result"], "skipped")
        self.assertEqual((report["latest"], report["latest_eligible"], report["latest_published_at"]), ("9.9.9", "", NOW - DAY))
        state = self.state()
        self.au.record(self.rt, state, report, dry_run=False)
        self.assertEqual(state["items"]["panel"]["latest_published_at"], NOW - DAY)
        # 之后的检查不带发布时间：旧的时间要清掉，避免“还差几天”算错
        later = self.au.make_result("panel", "up_to_date", "x", current="9.9.9", latest="9.9.9")
        later.update(latest_eligible="9.9.9", latest_published_at=None)
        self.au.record(self.rt, state, later, dry_run=False)
        self.assertNotIn("latest_published_at", state["items"]["panel"])
        self.assertEqual(state["items"]["panel"]["latest_eligible"], "9.9.9")


class AutoUpdateApiTest(AutoUpdateTestBase):
    def test_payload_has_ui_updated_at_from_index_mtime(self):
        self.app.mihomo_api_get = lambda path, timeout=2: (True, {"version": "v1.19.35"})
        ui = self.dir / "ui"
        ui.mkdir(exist_ok=True)
        (ui / "index.html").write_text("<html></html>")
        os.utime(ui / "index.html", (NOW - 3 * DAY, NOW - 3 * DAY))
        payload = self.app.auto_update_status_payload()
        self.assertEqual(payload["ui_updated_at"], NOW - 3 * DAY)
        self.assertIn("server_time", payload)
        (ui / "index.html").unlink()
        self.assertEqual(self.app.auto_update_status_payload()["ui_updated_at"], 0)


class StalePageBannerTest(unittest.TestCase):
    def test_meta_tag_and_index_render_version(self):
        html = index_text()
        self.assertIn('<meta name="mihomo-panel-version" content="{{ panel_version }}">', html)
        app = APP.read_text(encoding="utf-8")
        index_route = app[app.index("@app.route('/')"):app.index("@app.route('/api/status')")]
        self.assertIn("render_template('index.html', panel_version=PANEL_VERSION)", index_route)

    def test_banner_markup_and_wiring(self):
        html = index_text()
        self.assertIn('id="stalePageBanner"', html)
        self.assertIn('onclick="location.reload()"', html)
        self.assertRegex(html, r"\.stale-page-banner \{[^}]*position: fixed; top: 0")
        render_overview = html[html.index("function renderOverview("):html.index("function formatLastSubscription(")]
        self.assertIn("checkStalePage(data.panel_version)", render_overview)
        self.assertIn("meta[name=\"mihomo-panel-version\"]", html)

    def test_stale_message_cases(self):
        js = extract(index_text(), COMPARE, STALE)
        out = run_node(self, js, "stalePageMessage(c[0], c[1])", [
            ["0.1.38", "0.1.39"],
            ["0.1.39", "0.1.39"],
            ["0.1.39", "0.1.38"],
            ["", "0.1.39"],
            ["{{ panel_version }}", "0.1.39"],
            ["0.1.39", ""],
        ])
        self.assertEqual(out[0], "面板已升级到 v0.1.39，当前页面是旧版本，请刷新")
        self.assertEqual(out[1], "")
        self.assertEqual(out[2], "面板版本已变为 v0.1.38，当前页面是旧版本，请刷新")
        self.assertEqual(out[3:], ["", "", ""])


if __name__ == "__main__":
    unittest.main()
