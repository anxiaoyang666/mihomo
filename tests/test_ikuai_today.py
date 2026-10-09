"""设备页（爱快模式）：在线终端的今日流量由面板累加相邻两次读数的增量，最近活动在线时就是现在。"""
import tempfile
import unittest

from test_rule_sync_render import load_app

DAY1 = 1791504000 - 8 * 3600 + 3600   # 北京时间 2026-10-09 01:00
GB = 1024 ** 3


def client(mac, total, today=0, ip="10.10.10.50"):
    return {"ip_addr": ip, "mac": mac, "total_up": total // 2, "total_down": total - total // 2, "today_total": today,
            "timestamp": DAY1 - 86400 * 30}


class IkuaiTodayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def today(self, c, now):
        with self.app.IKUAI_LOCK:
            return self.app.ikuai_today_total(c, now)

    def update(self, clients, now):
        with self.app.IKUAI_LOCK:
            self.app.update_ikuai_today(clients, now)

    def test_accumulates_increments_and_rolls_over(self):
        self.update([client("aa", 1000)], DAY1)
        self.assertEqual(self.today(client("aa", 1000), DAY1), 0, "第一次只记读数")
        self.update([client("aa", 1500)], DAY1 + 10)
        self.update([client("aa", 1800)], DAY1 + 20)
        self.assertEqual(self.today(client("aa", 1800), DAY1 + 20), 800)
        self.assertEqual(self.today(client("aa", 1800, today=900), DAY1 + 20), 900, "爱快自己报了更大的数就用它")
        self.update([client("aa", 2000)], DAY1 + 86400)
        self.assertEqual(self.today(client("aa", 2000), DAY1 + 86400), 200, "换日后从新的一天重新累加（跨日那一次采样的增量算到新的一天）")
        self.update([client("aa", 2600)], DAY1 + 86410)
        self.assertEqual(self.today(client("aa", 2600), DAY1 + 86410), 800)

    def test_glitch_zero_reading_does_not_count_whole_total(self):
        # 爱快偶尔把累计报成 0 再恢复：不能把 170 GB 都算进今天
        self.update([client("mm", 170 * GB)], DAY1)
        self.update([client("mm", 0)], DAY1 + 10)
        self.update([client("mm", 170 * GB + 5000)], DAY1 + 20)
        self.update([client("mm", 170 * GB + 9000)], DAY1 + 30)
        self.assertEqual(self.today(client("mm", 0), DAY1 + 30), 4000)

    def test_router_reset_counts_from_new_counter(self):
        self.update([client("aa", 10 * GB)], DAY1)
        self.update([client("aa", 300)], DAY1 + 10)   # 路由器重启清零
        self.update([client("aa", 800)], DAY1 + 20)
        self.assertEqual(self.today(client("aa", 800), DAY1 + 20), 500)

    def test_unknown_or_stale_day_uses_reported(self):
        self.assertEqual(self.today(client("bb", 5000, today=7), DAY1), 7)
        self.update([client("aa", 1000)], DAY1)
        self.assertEqual(self.today(client("aa", 5000, today=3), DAY1 + 86400), 3, "还没换日时不拿昨天的数")

    def test_snapshot_rows_and_persistence(self):
        app = self.app
        self.update([client("aa", 1000)], DAY1)
        self.update([client("aa", 1500)], DAY1 + 10)
        rows = app.ikuai_device_rows([client("aa", 1800)], [], [], {}, {}, set(), set(), DAY1 + 60, {})
        self.assertEqual(rows[0]["last_seen"], DAY1 + 60, "在线时最近活动就是现在，不是爱快的上线时间")
        app.TRAFFIC_DAILY.update({"date": "2026-10-09", "down": 1, "up": 1})
        path = self.tmp.name + "/traffic_daily.json"
        app.save_traffic_daily(path)
        fresh = load_app(self.tmp.name)
        fresh.load_traffic_daily(path)
        self.assertEqual(fresh.IKUAI_TODAY, {"date": "2026-10-09", "last": {"aa": 1500}, "today": {"aa": 500}})

    def test_wired(self):
        self.assertIn("ikuai_today_total", self.app.ikuai_devices_snapshot.__code__.co_names)
        self.assertIn("update_ikuai_today", self.app.poll_ikuai.__code__.co_names)


if __name__ == "__main__":
    unittest.main()
