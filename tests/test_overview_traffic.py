"""概览页：代理组健康（按配置顺序、顺着当前选择找到实际节点和它的延迟）与实时网速曲线的采样。"""
import tempfile
import unittest

from test_rule_sync_render import ROOT, load_app

INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"


def node(delays):
    return {"type": "Shadowsocks", "history": [{"delay": d} for d in delays]}


def group(kind, now, members):
    return {"type": kind, "now": now, "all": members, "history": []}


PROXIES = {"proxies": {
    "Final": group("Selector", "♻️ 自动选择", ["♻️ 自动选择"]),
    "GLOBAL": group("Selector", "♻️ 自动选择", ["DIRECT", "DMIT", "BWG", "♻️ 自动选择", "🪜 DMIT", "AI", "Final"]),
    "AI": group("Selector", "🪜 DMIT", ["🪜 DMIT", "BWG"]),
    "♻️ 自动选择": group("Fallback", "🪜 DMIT", ["🪜 DMIT", "BWG"]),
    "🪜 DMIT": group("Fallback", "DMIT", ["DMIT"]),
    "DMIT": node([150, 163]),
    "BWG": node([190, 0]),
    "DIRECT": {"type": "Direct", "history": []},
}}


class ProxyGroupSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = load_app(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_order_leaf_and_delay(self):
        groups = self.app.proxy_group_summary(PROXIES)
        self.assertEqual([g["name"] for g in groups], ["♻️ 自动选择", "🪜 DMIT", "AI", "Final"], "按配置顺序，不含 GLOBAL")
        by_name = {g["name"]: g for g in groups}
        self.assertEqual(by_name["Final"]["leaf"], "DMIT")
        self.assertEqual(by_name["Final"]["delay"], 163, "分组的延迟取实际节点最近一次测速")
        self.assertEqual(by_name["🪜 DMIT"]["leaf"], "", "当前选择已经是节点时不重复显示")

    def test_timeout_is_zero(self):
        proxies = {"proxies": dict(PROXIES["proxies"], AI=group("Selector", "BWG", ["BWG"]))}
        ai = next(g for g in self.app.proxy_group_summary(proxies) if g["name"] == "AI")
        self.assertEqual(ai["delay"], 0, "最近一次超时记为 0，前端显示“超时”")

    def test_no_history_and_loops(self):
        self.assertIsNone(self.app.latest_delay({"history": []}))
        loop = {"A": group("Selector", "B", ["B"]), "B": group("Selector", "A", ["A"])}
        self.assertIn(self.app.resolve_group_leaf(loop, "A"), ("A", "B"))


class TrafficSeriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rates(self):
        record = self.app.record_traffic_sample
        record({"downloadTotal": 1000, "uploadTotal": 100}, now=100)
        self.assertEqual(self.app.traffic_series(), [], "第一次只记基准")
        record({"downloadTotal": 11000, "uploadTotal": 600}, now=105)
        self.assertEqual(self.app.traffic_series(), [{"t": 105, "down": 2000, "up": 100}])
        record({"downloadTotal": 50, "uploadTotal": 10}, now=110)
        self.assertEqual(len(self.app.traffic_series()), 1, "mihomo 重启后累计值变小，这一轮不算")
        record({"downloadTotal": 5050, "uploadTotal": 10}, now=500)
        self.assertEqual(len(self.app.traffic_series()), 1, "间隔太久（控制器断过）不算")
        for i in range(200):
            record({"downloadTotal": 5050 + i, "uploadTotal": 10}, now=505 + 5 * i)
        self.assertEqual(len(self.app.traffic_series()), self.app.TRAFFIC_SERIES_POINTS)

    def test_listener_inbound_is_swapped(self):
        record = self.app.record_traffic_sample
        def conn(cid, name, up, down):
            return {"id": cid, "upload": up, "download": down, "metadata": {"inboundName": name}}
        record({"downloadTotal": 0, "uploadTotal": 0, "connections": [conn("a", "ss-inbound", 0, 0)]}, now=100)
        # 其他地点经隧道往本地推了 10000 字节（mihomo 记为上传），本机 TUN 客户端上传 500、下载 2000
        record({"downloadTotal": 2010, "uploadTotal": 10500, "connections": [
            conn("a", "ss-inbound", 10000, 10), conn("b", "DEFAULT-TUN", 500, 2000)]}, now=105)
        self.assertEqual(self.app.traffic_series(), [{"t": 105, "down": 2400, "up": 102}])
        self.assertFalse(self.app.is_listener_inbound({"metadata": {"inboundName": "DEFAULT-MIXED"}}))
        self.assertFalse(self.app.is_listener_inbound({"metadata": {}}))

    def test_daily_totals_restart_and_rollover(self):
        app = self.app
        record = app.record_traffic_sample
        day1 = 1791504000 - app.TRAFFIC_DAY_OFFSET + 3600   # 北京时间 10-09 01:00
        def conn(cid, up, down):
            return {"id": cid, "upload": up, "download": down, "metadata": {"inboundName": "ss-inbound"}}
        record({"downloadTotal": 100, "uploadTotal": 100, "connections": []}, now=day1)
        record({"downloadTotal": 1100, "uploadTotal": 5100, "connections": [conn("a", 4000, 0)]}, now=day1 + 5)
        today = app.traffic_daily_summary(now=day1 + 5)["today"]
        self.assertEqual((today["date"], today["down"], today["up"]), ("2026-10-09", 5000, 1000), "隧道进来的 4000 算下载")
        # mihomo 重启：计数器从 0 开始，重启后的量照样算进今天
        record({"downloadTotal": 300, "uploadTotal": 50, "connections": []}, now=day1 + 10)
        self.assertEqual(app.traffic_daily_summary(now=day1 + 10)["today"]["down"], 5300)
        # 第二天
        summary = app.traffic_daily_summary(now=day1 + 86400)
        self.assertEqual(summary["today"], {"date": "2026-10-10", "down": 0, "up": 0})
        self.assertEqual(summary["yesterday"], {"date": "2026-10-09", "down": 5300, "up": 1050})
        # 隔了不止一天：没有昨天的数据
        self.assertIsNone(app.traffic_daily_summary(now=day1 + 3 * 86400)["yesterday"])

    def test_daily_persistence(self):
        app = self.app
        app.TRAFFIC_DAILY.update({"date": "2026-10-09", "down": 7, "up": 8, "yesterday": {"date": "2026-10-08", "down": 1, "up": 2}})
        path = self.tmp.name + "/traffic_daily.json"
        self.assertTrue(app.save_traffic_daily(path))
        fresh = load_app(self.tmp.name)
        self.assertTrue(fresh.load_traffic_daily(path))
        self.assertEqual((fresh.TRAFFIC_DAILY["down"], fresh.TRAFFIC_DAILY["yesterday"]["up"]), (7, 2))
        with open(path, "w") as f:
            f.write("{bad")
        self.assertFalse(fresh.load_traffic_daily(path))

    def test_breakdown_by_exit_and_rule(self):
        app = self.app
        record = app.record_traffic_sample
        def conn(cid, up, down, chains, rule, payload, inbound="DEFAULT-TUN"):
            return {"id": cid, "upload": up, "download": down, "chains": chains, "rule": rule, "rulePayload": payload,
                    "metadata": {"inboundName": inbound}}
        record({"downloadTotal": 0, "uploadTotal": 0, "connections": [
            conn("a", 0, 0, ["DMIT", "🪜 DMIT", "♻️ 自动选择"], "RuleSet", "proxy")]}, now=100)
        record({"downloadTotal": 9000, "uploadTotal": 1500, "connections": [
            conn("a", 100, 5000, ["DMIT", "🪜 DMIT", "♻️ 自动选择"], "RuleSet", "proxy"),
            conn("b", 400, 3000, ["DIRECT"], "IPCIDR", "10.10.10.0/24"),
            conn("c", 1000, 1000, ["DIRECT"], "Match", "", inbound="ss-inbound")]}, now=105)
        result = app.traffic_breakdown()
        exits = {row["name"]: (row["down"], row["up"]) for row in result["exits"]}
        self.assertEqual(exits["DMIT"], (5000, 100))
        self.assertEqual(exits["DIRECT"], (3000 + 1000, 400 + 1000), "隧道进来的连接方向对调后算到 DIRECT")
        rules = {row["name"]: row for row in result["rules"]}
        self.assertEqual(rules["RuleSet(proxy)"]["group"], "♻️ 自动选择")
        self.assertIn("其他站点经隧道访问本地", rules)
        self.assertEqual(result["covered"], 5100 + 3400 + 2000)
        # 第二天清零
        app.add_daily_traffic(105 + 86400, 0, 0)
        self.assertEqual(app.traffic_breakdown()["exits"], [])

    def test_breakdown_caps_keys(self):
        app = self.app
        app.TRAFFIC_BREAKDOWN_KEYS = 3
        bucket = {}
        for i in range(5):
            app.add_breakdown(bucket, f"k{i}", 1, 1)
        self.assertEqual(sorted(bucket), ["k0", "k1", "k2", "其他"])
        self.assertEqual(bucket["其他"], [2, 2])

    def test_wired_into_overview_and_page(self):
        source = (ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"traffic_series": traffic_series(),', source)
        self.assertIn('"traffic_daily": traffic_daily_summary(),', source)
        self.assertIn('"traffic_breakdown": traffic_breakdown(),', source)
        self.assertIn("load_traffic_daily()", source)
        self.assertIn("save_traffic_daily()", source)
        self.assertIn("record_traffic_sample(data)", source)
        page = INDEX.read_text(encoding="utf-8")
        self.assertIn("renderTrafficChart(data.traffic_series);", page)
        self.assertNotIn("renderTrafficBars", page)
        self.assertIn("group.delay === 0 ? '超时'", page)


class LogSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.app.LOG_FILE = self.tmp.name + "/mihomo.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_counts_and_recent(self):
        lines = [
            'time="2026-10-09T03:00:00.1Z" level=warning msg="old warning"',
            'time="2026-10-09T05:40:16.5Z" level=warning msg="[TCP] dial DIRECT error: dns resolve failed"',
            'time="2026-10-09T05:40:20.5Z" level=info msg="[TCP] a --> b"',
            'time="2026-10-09T05:41:00.5Z" level=error msg="boom \\"x\\""',
            'not a log line',
        ]
        with open(self.app.LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        now = self.app.parse_log_epoch("2026-10-09T05:45:00")
        summary = self.app.log_level_summary(now=now)
        self.assertEqual((summary["error"], summary["warn"], summary["info"]), (1, 1, 1), "只算最近 1 小时")
        self.assertEqual([r["level"] for r in summary["recent"]], ["error", "warn"], "最新的在前")
        self.assertEqual(summary["recent"][0]["message"], 'boom "x"')

    def test_missing_file(self):
        summary = self.app.log_level_summary()
        self.assertEqual((summary["error"], summary["recent"]), (0, []))


if __name__ == "__main__":
    unittest.main()
