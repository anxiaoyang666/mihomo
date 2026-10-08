"""设备流量统计：真的往聚合器里喂 /connections 快照，检查增量、归一化、持久化和备注校验。

其他测试只 grep 源码，这里按 test_rule_sync_render.py 的方式 exec app.py（Flask 用桩、MIHOMO_DIR 指到临时目录），
直接调用 apply_connections_sample 等函数。
"""
from pathlib import Path
import json
import os
import sys
import tempfile
import threading
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"
UNIT = ROOT / "remote-root" / "etc" / "systemd" / "system" / "mihomo-manager.service"


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
    module = types.ModuleType("mihomo_app_devices_test")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


def conn(cid, src, up, down, host="example.com", chains=None, start="2026-10-08T10:00:00Z", dest="1.2.3.4"):
    return {
        "id": cid,
        "metadata": {"sourceIP": src, "sourcePort": "50000", "host": host, "destinationIP": dest, "network": "tcp", "type": "TUN"},
        "upload": up,
        "download": down,
        "start": start,
        "chains": chains if chains is not None else ["DIRECT"],
        "rule": "Match",
        "rulePayload": "",
    }


class DeviceAggregatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def devices(self):
        return self.app.DEVICE_STATE["devices"]

    def test_deltas_new_growth_disappearance_and_negative(self):
        app = self.app
        local = {"127.0.0.1", "::1", "10.0.0.1"}
        # 第一轮：两条连接，新 id 按当前值全额计入
        app.apply_connections_sample([
            conn("a", "10.0.0.5", 100, 1000, host="a.com"),
            conn("b", "10.0.0.5", 50, 500, host="b.com"),
        ], now=1000, local_ips=local)
        dev = self.devices()["10.0.0.5"]
        self.assertEqual(dev["upload_total"], 150)
        self.assertEqual(dev["download_total"], 1500)
        self.assertEqual(dev["active_connections"], 2)
        self.assertEqual(dev["first_seen"], 1000)
        self.assertEqual(dev["last_seen"], 1000)
        # 首轮没有上一次采样时间，按默认间隔算速率
        self.assertEqual(dev["rate_down"], 1500 // app.DEVICE_SAMPLE_INTERVAL)

        # 第二轮：a 增长，b 消失（它之前累加的字节要保留），c 是新连接
        app.apply_connections_sample([
            conn("a", "10.0.0.5", 130, 1200, host="a.com"),
            conn("c", "10.0.0.5", 10, 20, host="c.com"),
        ], now=1005, local_ips=local)
        dev = self.devices()["10.0.0.5"]
        self.assertEqual(dev["upload_total"], 150 + 30 + 10)
        self.assertEqual(dev["download_total"], 1500 + 200 + 20)
        self.assertEqual(dev["active_connections"], 2)
        self.assertEqual(dev["rate_up"], (30 + 10) // 5)
        self.assertEqual(dev["rate_down"], (200 + 20) // 5)
        self.assertEqual(dev["last_seen"], 1005)
        self.assertNotIn("b", app.DEVICE_STATE["prev"])
        # b.com 的字节还在域名表里
        self.assertEqual(dev["domains"]["b.com"]["bytes"], 550)

        # 第三轮：a 的计数器变小（id 被复用），按新连接全额计入
        app.apply_connections_sample([
            conn("a", "10.0.0.5", 5, 7, host="a.com"),
        ], now=1010, local_ips=local)
        dev = self.devices()["10.0.0.5"]
        self.assertEqual(dev["upload_total"], 190 + 5)
        self.assertEqual(dev["download_total"], 1720 + 7)
        self.assertEqual(dev["domains"]["a.com"]["count"], 2)
        self.assertEqual(dev["active_connections"], 1)

    def test_ipv4_mapped_source_is_normalised(self):
        self.app.apply_connections_sample([conn("x", "::ffff:192.168.1.20", 1, 2)], now=1000, local_ips=set())
        self.assertIn("192.168.1.20", self.devices())
        self.assertNotIn("::ffff:192.168.1.20", self.devices())
        self.assertEqual(self.app.normalize_source_ip("[::1]"), "::1")
        self.assertEqual(self.app.normalize_source_ip("fe80::1"), "fe80::1")

    def test_gateway_traffic_is_aggregated_into_pseudo_device(self):
        local = {"127.0.0.1", "::1", "10.0.0.1"}
        self.app.apply_connections_sample([
            conn("l1", "127.0.0.1", 10, 20, host="dns.google"),
            conn("l2", "::ffff:10.0.0.1", 1, 2, host="dns.google"),
            conn("l3", "::1", 100, 200, host="doh.pub"),
            conn("d1", "10.0.0.9", 5, 5),
        ], now=1000, local_ips=local)
        devices = self.devices()
        self.assertNotIn("127.0.0.1", devices)
        self.assertNotIn("10.0.0.1", devices)
        gateway = devices[self.app.GATEWAY_DEVICE_KEY]
        self.assertTrue(gateway["is_gateway"])
        self.assertEqual(gateway["upload_total"], 111)
        self.assertEqual(gateway["download_total"], 222)
        self.assertEqual(gateway["active_connections"], 3)
        self.assertFalse(devices["10.0.0.9"]["is_gateway"])
        snapshot = self.app.devices_snapshot(now=1000)
        gateway_item = next(item for item in snapshot["devices"] if item["is_gateway"])
        self.assertEqual(gateway_item["ip"], self.app.GATEWAY_DEVICE_KEY)

    def test_online_flag_follows_last_seen(self):
        app = self.app
        app.apply_connections_sample([conn("a", "10.0.0.5", 1, 1), conn("b", "10.0.0.6", 1, 1)], now=1000, local_ips=set())
        app.apply_connections_sample([conn("a", "10.0.0.5", 2, 2)], now=1000 + app.DEVICE_ONLINE_SECONDS + 1, local_ips=set())
        snapshot = app.devices_snapshot(now=1000 + app.DEVICE_ONLINE_SECONDS + 1)
        by_ip = {item["ip"]: item for item in snapshot["devices"]}
        self.assertTrue(by_ip["10.0.0.5"]["online"])
        self.assertFalse(by_ip["10.0.0.6"]["online"])
        self.assertEqual(by_ip["10.0.0.6"]["active_connections"], 0)
        self.assertEqual(by_ip["10.0.0.6"]["rate_down"], 0)
        # 在线的排在前面
        self.assertEqual(snapshot["devices"][0]["ip"], "10.0.0.5")
        self.assertEqual(snapshot["totals"], {
            "devices": 2, "online": 1, "upload": 3, "download": 3,
            "rate_up": by_ip["10.0.0.5"]["rate_up"], "rate_down": by_ip["10.0.0.5"]["rate_down"],
        })
        self.assertEqual(app.devices_summary(now=1000 + app.DEVICE_ONLINE_SECONDS + 1), {"online": 1, "total": 2})
        self.assertEqual(snapshot["sample_interval"], app.DEVICE_SAMPLE_INTERVAL)
        self.assertTrue(snapshot["controller"]["reachable"])

    def test_chain_label_shows_group_then_exit_node(self):
        app = self.app
        app.apply_connections_sample([
            conn("a", "10.0.0.5", 10, 10, chains=["🇭🇰 HK 01", "♻️ 自动选择", "🚀 默认代理"]),
            conn("b", "10.0.0.5", 1, 1, chains=["DIRECT"]),
            conn("c", "10.0.0.5", 1, 1, chains=[]),
        ], now=1000, local_ips=set())
        chains = self.devices()["10.0.0.5"]["chains"]
        self.assertEqual(chains["🚀 默认代理 → ♻️ 自动选择 → 🇭🇰 HK 01"], 20)
        self.assertEqual(chains["DIRECT"], 4)
        top = app.devices_snapshot(now=1000)["devices"][0]["chains"]
        self.assertEqual(top[0]["label"], "🚀 默认代理 → ♻️ 自动选择 → 🇭🇰 HK 01")

    def test_persistence_round_trip(self):
        app = self.app
        app.apply_connections_sample([conn("a", "10.0.0.5", 100, 200, host="a.com")], now=1000, local_ips=set())
        ok, _ = app.set_device_note("10.0.0.5", "客厅电视")
        self.assertTrue(ok)
        app.save_device_state()
        path = os.path.join(self.tmp.name, "devices.json")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertFalse([name for name in os.listdir(self.tmp.name) if name.endswith(".tmp")])
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["notes"], {"10.0.0.5": "客厅电视"})
        self.assertEqual(data["devices"]["10.0.0.5"]["download_total"], 200)

        # 模拟面板重启：新模块从文件读回
        fresh = load_app(self.tmp.name)
        self.assertTrue(fresh.load_device_state())
        dev = fresh.DEVICE_STATE["devices"]["10.0.0.5"]
        self.assertEqual(dev["upload_total"], 100)
        self.assertEqual(dev["download_total"], 200)
        self.assertEqual(dev["domains"]["a.com"]["bytes"], 300)
        self.assertEqual(fresh.DEVICE_STATE["notes"]["10.0.0.5"], "客厅电视")
        # 重启后同一条连接 id 又出现，没有 prev 就按新连接全额计入
        fresh.apply_connections_sample([conn("a", "10.0.0.5", 100, 200, host="a.com")], now=2000, local_ips=set())
        self.assertEqual(fresh.DEVICE_STATE["devices"]["10.0.0.5"]["download_total"], 400)
        item = fresh.devices_snapshot(now=2000)["devices"][0]
        self.assertEqual(item["note"], "客厅电视")

    def test_corrupt_state_file_is_ignored(self):
        path = os.path.join(self.tmp.name, "devices.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(self.app.load_device_state())
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"devices": {"10.0.0.5": {"upload_total": "abc", "domains": "nope"}, "bad": None}, "notes": {"x": 5}}, f)
        self.assertTrue(self.app.load_device_state())
        self.assertEqual(self.app.DEVICE_STATE["devices"]["10.0.0.5"]["upload_total"], 0)
        self.assertNotIn("bad", self.app.DEVICE_STATE["devices"])
        self.assertEqual(self.app.DEVICE_STATE["notes"], {})

    def test_domain_cap_evicts_oldest(self):
        app = self.app
        limit = app.DEVICE_MAX_DOMAINS
        for index in range(limit):
            app.apply_connections_sample([conn(f"c{index}", "10.0.0.5", 1, 1, host=f"h{index}.com")], now=1000 + index, local_ips=set())
        self.assertEqual(len(self.devices()["10.0.0.5"]["domains"]), limit)
        app.apply_connections_sample([conn("new", "10.0.0.5", 1, 1, host="newest.com")], now=5000, local_ips=set())
        domains = self.devices()["10.0.0.5"]["domains"]
        self.assertEqual(len(domains), limit)
        self.assertIn("newest.com", domains)
        self.assertNotIn("h0.com", domains)
        self.assertIn("h1.com", domains)

    def test_device_cap_evicts_oldest_but_keeps_gateway(self):
        app = self.app
        app.apply_connections_sample([conn("g", "127.0.0.1", 1, 1)], now=1, local_ips={"127.0.0.1"})
        for index in range(app.DEVICE_MAX_DEVICES):
            app.apply_connections_sample([conn(f"c{index}", f"10.{(index >> 8) & 255}.{index & 255}.1", 1, 1)], now=100 + index, local_ips={"127.0.0.1"})
        devices = self.devices()
        self.assertEqual(len(devices), app.DEVICE_MAX_DEVICES)
        self.assertIn(app.GATEWAY_DEVICE_KEY, devices)
        self.assertNotIn("10.0.0.1", devices)

    def test_top_domains_limited_to_twelve(self):
        app = self.app
        sample = [conn(f"c{index}", "10.0.0.5", index, 0, host=f"h{index}.com") for index in range(20)]
        app.apply_connections_sample(sample, now=1000, local_ips=set())
        item = app.devices_snapshot(now=1000)["devices"][0]
        self.assertEqual(len(item["top_domains"]), 12)
        self.assertEqual(item["top_domains"][0]["host"], "h19.com")

    def test_reset_keeps_notes_and_devices(self):
        app = self.app
        app.apply_connections_sample([conn("a", "10.0.0.5", 100, 200)], now=1000, local_ips=set())
        app.set_device_note("10.0.0.5", "NAS")
        app.reset_device_totals()
        dev = self.devices()["10.0.0.5"]
        self.assertEqual((dev["upload_total"], dev["download_total"], dev["domains"], dev["chains"]), (0, 0, {}, {}))
        self.assertEqual(app.DEVICE_STATE["notes"]["10.0.0.5"], "NAS")
        # 清零后同一条连接继续增长时只计增量
        app.apply_connections_sample([conn("a", "10.0.0.5", 110, 230)], now=1005, local_ips=set())
        self.assertEqual((dev["upload_total"], dev["download_total"]), (10, 30))

    def test_note_validation(self):
        app = self.app
        self.assertFalse(app.set_device_note("not-an-ip", "x")[0])
        self.assertFalse(app.set_device_note("10.0.0.5; rm -rf /", "x")[0])
        self.assertFalse(app.set_device_note("10.0.0.5", "a" * (app.DEVICE_NOTE_MAX_LEN + 1))[0])
        self.assertFalse(app.set_device_note("10.0.0.5", "line\nbreak")[0])
        self.assertFalse(app.set_device_note("10.0.0.5", "nul\x00byte")[0])
        self.assertFalse(app.set_device_note("10.0.0.5", 123)[0])
        self.assertTrue(app.set_device_note("10.0.0.5", "a" * app.DEVICE_NOTE_MAX_LEN)[0])
        self.assertTrue(app.set_device_note("::ffff:10.0.0.6", " 书房 ")[0])
        self.assertEqual(app.DEVICE_STATE["notes"]["10.0.0.6"], "书房")
        self.assertTrue(app.set_device_note("fe80::1", "v6")[0])
        # 空备注等于删除
        self.assertTrue(app.set_device_note("10.0.0.6", "")[0])
        self.assertNotIn("10.0.0.6", app.DEVICE_STATE["notes"])

    def test_controller_failure_keeps_totals_and_records_error(self):
        app = self.app
        app.apply_connections_sample([conn("a", "10.0.0.5", 100, 200)], now=1000, local_ips=set())
        app.mihomo_api_get = lambda path, timeout=2: (False, {"error": "connection refused", "url": "http://127.0.0.1:9090/connections"})
        self.assertFalse(app.sample_devices_once())
        snapshot = app.devices_snapshot(now=1001)
        self.assertFalse(snapshot["controller"]["reachable"])
        self.assertEqual(snapshot["controller"]["error"], "connection refused")
        self.assertEqual(snapshot["devices"][0]["download_total"], 200)
        self.assertEqual(snapshot["devices"][0]["rate_down"], 0)
        # 恢复后错误清掉，增量照常
        app.mihomo_api_get = lambda path, timeout=2: (True, {"connections": [conn("a", "10.0.0.5", 100, 260)]})
        self.assertTrue(app.sample_devices_once())
        snapshot = app.devices_snapshot()
        self.assertTrue(snapshot["controller"]["reachable"])
        self.assertEqual(snapshot["devices"][0]["download_total"], 260)

    def test_neighbour_parsing(self):
        app = self.app
        app.run_args = lambda args, timeout=30: (True, "10.0.0.5 dev eth0 lladdr aa:BB:cc:dd:ee:ff REACHABLE\n10.0.0.7 dev eth0 FAILED\nfe80::1 dev eth0 lladdr 11:22:33:44:55:66 router STALE\n")
        self.assertEqual(app.detect_neighbour_macs(), {"10.0.0.5": "aa:bb:cc:dd:ee:ff", "fe80::1": "11:22:33:44:55:66"})
        app.run_args = lambda args, timeout=30: (True, "1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 10.0.0.1/24 brd 10.0.0.255 scope global eth0\n")
        self.assertEqual(app.detect_local_ips(), {"127.0.0.1", "::1", "10.0.0.1"})
        app.run_args = lambda args, timeout=30: (False, "not found")
        self.assertEqual(app.detect_local_ips(), {"127.0.0.1", "::1"})
        self.assertEqual(app.detect_neighbour_macs(), {})


class DevicesContractTest(unittest.TestCase):
    def test_sampler_not_started_at_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(tmp)
            self.assertFalse(app.DEVICE_SAMPLER_STARTED)
            self.assertNotIn("device-sampler", [thread.name for thread in threading.enumerate()])
        text = APP.read_text(encoding="utf-8")
        self.assertNotIn("\nstart_device_sampler()\n", text)
        main_block = text[text.find("if __name__ == '__main__':"):]
        self.assertIn("start_device_sampler()", main_block)
        self.assertLess(main_block.find("start_device_sampler()"), main_block.find("app.run("))
        self.assertIn("signal.signal(signal.SIGTERM", main_block)
        # 服务单元确实把 app.py 当 __main__ 跑
        self.assertIn("ExecStart=/usr/bin/python3 /etc/mihomo/manager/app.py", UNIT.read_text(encoding="utf-8"))

    def test_backend_routes_and_overview_summary(self):
        text = APP.read_text(encoding="utf-8")
        self.assertIn("@app.route('/api/devices')", text)
        self.assertIn("@app.route('/api/devices/reset', methods=['POST'])", text)
        self.assertIn("@app.route('/api/devices/<ip>/note', methods=['POST'])", text)
        self.assertIn('"devices": devices_summary()', text)
        self.assertIn("DEVICE_SAMPLE_INTERVAL = 5", text)
        self.assertIn("DEVICE_ONLINE_SECONDS = 90", text)
        self.assertIn('DEVICES_FILE = f"{MIHOMO_DIR}/devices.json"', text)
        self.assertIn("DEVICE_LOCK = threading.Lock()", text)
        self.assertIn('mihomo_api_get("/connections", timeout=3)', text)
        # 三个设备接口都要登录
        for route in ("@app.route('/api/devices')", "@app.route('/api/devices/reset'", "@app.route('/api/devices/<ip>/note'"):
            after = text[text.find(route):text.find(route) + 200]
            self.assertIn("@login_required", after)

    def test_ui_has_tab_polling_cleanup_and_empty_state(self):
        text = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="tab-devices-btn"', text)
        self.assertIn('data-bs-target="#tab-devices"', text)
        self.assertIn('id="tab-devices"', text)
        self.assertIn("bi-pc-display", text)
        # 设备按钮排在概览和订阅之间
        self.assertLess(text.find('id="tab-dash-btn"'), text.find('id="tab-devices-btn"'))
        self.assertLess(text.find('id="tab-devices-btn"'), text.find('id="tab-tasks-btn"'))
        self.assertIn("let devicesInterval = null;", text)
        self.assertIn("clearInterval(devicesInterval)", text)
        self.assertIn("addEventListener('shown.bs.tab', startDevicePolling)", text)
        self.assertIn("addEventListener('hidden.bs.tab', stopDevicePolling)", text)
        self.assertIn("setInterval(() => loadDevices(), 5000)", text)
        self.assertIn("还没有采样到设备流量。面板启动后每 5 秒从 mihomo 控制器读取连接。", text)
        self.assertIn("deviceDetailsOpen", text)
        self.assertIn("deviceDirtyNotes", text)
        self.assertIn("deviceRowProtected", text)
        self.assertIn("api('/devices/reset'", text)
        self.assertIn("/devices/${encodeURIComponent(ip)}/note", text)
        self.assertIn('id="devicesPill"', text)
        self.assertIn("设备 在线 ${data.devices.online ?? 0} / 共 ${data.devices.total ?? 0}", text)
        self.assertIn("清零统计", text)


if __name__ == "__main__":
    unittest.main()
