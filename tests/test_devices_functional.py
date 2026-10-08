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
        expected_totals = {
            "devices": 2, "online": 1, "lan_total": 2, "lan_online": 1, "neighbour_only": 0, "upload": 3, "download": 3,
            "rate_up": by_ip["10.0.0.5"]["rate_up"], "rate_down": by_ip["10.0.0.5"]["rate_down"],
        }
        self.assertEqual({key: snapshot["totals"][key] for key in expected_totals}, expected_totals)
        self.assertEqual(app.devices_summary(now=1000 + app.DEVICE_ONLINE_SECONDS + 1), {"online": 1, "total": 2, "lan_online": 1, "lan_total": 2})
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
        neigh = "10.0.0.5 dev eth0 lladdr aa:BB:cc:dd:ee:ff REACHABLE\n10.0.0.7 dev eth0 FAILED\n10.0.0.8 dev eth0 INCOMPLETE\nfe80::1 dev eth0 lladdr 11:22:33:44:55:66 router STALE\n"
        self.assertEqual(app.parse_neighbours(neigh), {
            "10.0.0.5": {"mac": "aa:bb:cc:dd:ee:ff", "state": "REACHABLE"},
            "10.0.0.7": {"mac": "", "state": "FAILED"},
            "10.0.0.8": {"mac": "", "state": "INCOMPLETE"},
            "fe80::1": {"mac": "11:22:33:44:55:66", "state": "STALE"},
        })
        app.run_args = lambda args, timeout=30: (True, neigh)
        self.assertEqual(app.detect_neighbour_macs(), {"10.0.0.5": "aa:bb:cc:dd:ee:ff", "fe80::1": "11:22:33:44:55:66"})
        app.run_args = lambda args, timeout=30: (True, "default via 10.10.10.253 dev eth0 proto dhcp metric 100\n10.10.10.0/24 dev eth0 proto kernel scope link src 10.10.10.2\n")
        self.assertEqual(app.detect_upstream_router(), "10.10.10.253")
        app.run_args = lambda args, timeout=30: (True, "10.10.10.0/24 dev eth0 proto kernel scope link src 10.10.10.2\n")
        self.assertEqual(app.detect_upstream_router(), "")
        app.run_args = lambda args, timeout=30: (True, "1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 10.0.0.1/24 brd 10.0.0.255 scope global eth0\n")
        self.assertEqual(app.detect_local_ips(), {"127.0.0.1", "::1", "10.0.0.1"})
        app.run_args = lambda args, timeout=30: (False, "not found")
        self.assertEqual(app.detect_local_ips(), {"127.0.0.1", "::1"})
        self.assertEqual(app.detect_neighbour_macs(), {})
        self.assertEqual(app.detect_upstream_router(), "")

    def test_source_kind_classification(self):
        app = self.app
        local = {"127.0.0.1", "::1", "10.10.10.2"}
        self.assertEqual(app.source_kind("10.10.10.2", local), "gateway")
        self.assertEqual(app.source_kind("127.0.0.1", set()), "gateway")
        self.assertEqual(app.source_kind("10.10.10.3", local), "lan")
        self.assertEqual(app.source_kind("192.168.1.9", local), "lan")
        self.assertEqual(app.source_kind("172.16.5.5", local), "lan")
        self.assertEqual(app.source_kind("169.254.1.1", local), "lan")
        self.assertEqual(app.source_kind("100.64.0.9", local), "lan")
        self.assertEqual(app.source_kind("fd00::9", local), "lan")
        self.assertEqual(app.source_kind("122.6.190.2", local), "remote")
        self.assertEqual(app.source_kind("2606:4700::1111", local), "remote")
        self.assertEqual(app.source_kind("not-an-ip", local), "lan")

    def test_public_sources_grouped_into_remote_pseudo_device(self):
        app = self.app
        local = {"127.0.0.1", "::1", "10.10.10.2"}
        app.apply_connections_sample([
            conn("r1", "122.6.190.2", 100, 200, host="10.10.10.3", dest="10.10.10.3", chains=["DIRECT"]) | {"metadata": {"sourceIP": "122.6.190.2", "host": "", "destinationIP": "10.10.10.3", "type": "ShadowSocks", "inboundName": "ss-inbound"}},
            conn("r2", "::ffff:8.8.4.4", 1, 2),
            conn("l1", "10.10.10.3", 5, 5),
        ], now=1000, local_ips=local)
        devices = self.devices()
        self.assertNotIn("122.6.190.2", devices)
        self.assertNotIn("8.8.4.4", devices)
        remote = devices[app.REMOTE_DEVICE_KEY]
        self.assertEqual(remote["kind"], "remote")
        self.assertFalse(remote["is_gateway"])
        self.assertEqual(remote["upload_total"], 101)
        self.assertEqual(remote["download_total"], 202)
        self.assertEqual(set(remote["remote_ips"]), {"122.6.190.2", "8.8.4.4"})
        self.assertEqual(remote["inbounds"], {"ShadowSocks": 1, "TUN": 1})
        self.assertEqual(devices["10.10.10.3"]["kind"], "lan")
        # 再来 12 个新来源：接口只展示最近 10 个，计数是全部
        # 注意 203.0.113.0/24 这类文档网段在 ipaddress 里算 is_private，这里要用真正的公网地址
        sample = [conn(f"x{i}", f"122.6.191.{i}", 1, 1) for i in range(12)]
        app.apply_connections_sample(sample, now=2000, local_ips=local)
        item = next(item for item in app.devices_snapshot(now=2000)["devices"] if item["kind"] == "remote")
        self.assertEqual(item["ip"], app.REMOTE_DEVICE_KEY)
        self.assertEqual(item["remote_ip_count"], 14)
        self.assertEqual(len(item["remote_ips"]), 10)
        self.assertTrue(all(ip.startswith("122.6.191.") for ip in item["remote_ips"]))
        self.assertEqual(item["seen_via"], "traffic")
        self.assertIn("ShadowSocks", item["inbound_types"])
        # 远程伪设备不会被设备上限淘汰，也能落盘读回
        app.save_device_state()
        fresh = load_app(self.tmp.name)
        fresh.load_device_state()
        reloaded = fresh.DEVICE_STATE["devices"][app.REMOTE_DEVICE_KEY]
        self.assertEqual(reloaded["kind"], "remote")
        self.assertEqual(len(reloaded["remote_ips"]), 14)
        self.assertEqual(reloaded["inbounds"]["ShadowSocks"], 1)
        # 旧版 devices.json 没有 kind 字段：按 is_gateway / key 推断
        legacy = fresh.coerce_device_record("10.0.0.9", {"is_gateway": False, "upload_total": 1})
        self.assertEqual(legacy["kind"], "lan")
        self.assertEqual(fresh.coerce_device_record(fresh.GATEWAY_DEVICE_KEY, {"is_gateway": True})["kind"], "gateway")
        self.assertEqual(fresh.coerce_device_record(fresh.REMOTE_DEVICE_KEY, {})["kind"], "remote")

    def test_inbound_types_counted_per_connection(self):
        app = self.app
        def tun(cid, up=1, down=1):
            item = conn(cid, "10.10.10.253", up, down)
            item["metadata"]["type"] = "Tun"
            item["metadata"]["inboundName"] = "DEFAULT-TUN"
            return item
        def ss(cid):
            item = conn(cid, "10.10.10.253", 1, 1)
            item["metadata"]["type"] = "ShadowSocks"
            return item
        app.apply_connections_sample([tun("a"), tun("b"), ss("c")], now=1000, local_ips=set())
        # 同一条连接再出现不重复计数，新连接才加
        app.apply_connections_sample([tun("a", 5, 5), tun("d"), ss("c")], now=1005, local_ips=set())
        dev = self.devices()["10.10.10.253"]
        self.assertEqual(dev["inbounds"], {"Tun": 3, "ShadowSocks": 1})
        item = app.devices_snapshot(now=1005)["devices"][0]
        self.assertEqual(item["inbounds"], {"Tun": 3, "ShadowSocks": 1})
        self.assertEqual(item["inbound_types"], ["Tun", "ShadowSocks"])

    def fake_ip_commands(self, neigh="", route="", addr=""):
        def run(args, timeout=30):
            if args[:3] == ["ip", "-4", "neigh"]:
                return True, neigh
            if args[:2] == ["ip", "route"]:
                return True, route
            if args[:2] == ["ip", "-4"]:
                return True, addr
            return False, "unexpected " + " ".join(args)
        self.app.run_args = run

    def test_upstream_router_flagged(self):
        app = self.app
        self.fake_ip_commands(
            neigh="10.10.10.253 dev eth0 lladdr 00:11:22:33:44:55 REACHABLE\n",
            route="default via 10.10.10.253 dev eth0 proto dhcp src 10.10.10.2 metric 100\n10.10.10.0/24 dev eth0 proto kernel scope link src 10.10.10.2\n",
            addr="2: eth0    inet 10.10.10.2/24 brd 10.10.10.255 scope global eth0\n",
        )
        app.refresh_device_neighbours()
        self.assertEqual(app.DEVICE_STATE["upstream_router"], "10.10.10.253")
        self.assertEqual(app.DEVICE_STATE["local_ips"], {"127.0.0.1", "::1", "10.10.10.2"})
        app.apply_connections_sample([conn("a", "10.10.10.253", 1, 1), conn("b", "10.10.10.3", 1, 1)], now=1000)
        snapshot = app.devices_snapshot(now=1000)
        by_ip = {item["ip"]: item for item in snapshot["devices"]}
        self.assertTrue(by_ip["10.10.10.253"]["is_upstream_router"])
        self.assertEqual(by_ip["10.10.10.253"]["mac"], "00:11:22:33:44:55")
        self.assertEqual(by_ip["10.10.10.253"]["neighbour_state"], "REACHABLE")
        self.assertEqual(by_ip["10.10.10.253"]["kind"], "lan")
        self.assertFalse(by_ip["10.10.10.3"]["is_upstream_router"])
        self.assertEqual(snapshot["upstream_router"], "10.10.10.253")

    def test_neighbours_without_traffic_are_listed_but_not_persisted(self):
        app = self.app
        self.fake_ip_commands(
            neigh=(
                "10.10.10.3 dev eth0 lladdr aa:aa:aa:aa:aa:01 REACHABLE\n"      # 有流量的设备
                "10.10.10.20 dev eth0 lladdr aa:aa:aa:aa:aa:20 STALE\n"         # 只在 ARP 里
                "10.10.10.21 dev eth0 lladdr aa:aa:aa:aa:aa:21 REACHABLE\n"     # 只在 ARP 里
                "10.10.10.22 dev eth0 lladdr aa:aa:aa:aa:aa:22 DELAY\n"
                "10.10.10.30 dev eth0 FAILED\n"                                 # 跳过
                "10.10.10.31 dev eth0 INCOMPLETE\n"                             # 跳过
                "10.10.10.2 dev eth0 lladdr aa:aa:aa:aa:aa:02 PERMANENT\n"      # 网关自己，跳过
                "169.254.7.7 dev eth0 lladdr aa:aa:aa:aa:aa:77 REACHABLE\n"     # 链路本地，跳过
            ),
            route="default via 10.10.10.253 dev eth0\n",
            addr="2: eth0    inet 10.10.10.2/24 brd 10.10.10.255 scope global eth0\n",
        )
        app.refresh_device_neighbours()
        app.apply_connections_sample([conn("a", "10.10.10.3", 1, 1), conn("g", "10.10.10.2", 1, 1), conn("r", "122.6.190.2", 1, 1)], now=1000)
        app.set_device_note("10.10.10.21", "打印机")
        snapshot = app.devices_snapshot(now=1000)
        ips = [item["ip"] for item in snapshot["devices"]]
        # 有流量的在前（含伪设备），只在 ARP 里的按 IP 排在最后
        self.assertEqual(ips[-3:], ["10.10.10.20", "10.10.10.21", "10.10.10.22"])
        self.assertNotIn("10.10.10.30", ips)
        self.assertNotIn("10.10.10.31", ips)
        self.assertNotIn("10.10.10.2", ips)
        self.assertNotIn("169.254.7.7", ips)
        self.assertEqual(ips.count("10.10.10.3"), 1)
        by_ip = {item["ip"]: item for item in snapshot["devices"]}
        self.assertEqual(by_ip["10.10.10.3"]["seen_via"], "traffic")
        self.assertEqual(by_ip["10.10.10.3"]["neighbour_state"], "REACHABLE")
        row = by_ip["10.10.10.21"]
        self.assertEqual(row["seen_via"], "neighbour")
        self.assertEqual(row["kind"], "lan")
        self.assertFalse(row["online"])
        self.assertEqual(row["mac"], "aa:aa:aa:aa:aa:21")
        self.assertEqual(row["neighbour_state"], "REACHABLE")
        self.assertEqual(row["note"], "打印机")
        self.assertEqual((row["upload_total"], row["download_total"], row["active_connections"], row["rate_down"], row["last_seen"]), (0, 0, 0, 0, 0))
        self.assertEqual(row["top_domains"], [])
        self.assertEqual(by_ip["10.10.10.20"]["neighbour_state"], "STALE")
        # 口径：devices/online 只算有流量的；lan_* 算局域网（有流量 1 + ARP 3），伪设备不算
        totals = snapshot["totals"]
        self.assertEqual((totals["devices"], totals["online"]), (3, 3))
        self.assertEqual((totals["lan_total"], totals["lan_online"], totals["neighbour_only"]), (4, 1, 3))
        self.assertEqual(app.devices_summary(now=1000), {"online": 3, "total": 3, "lan_online": 1, "lan_total": 4})
        # 只在 ARP 里的设备不落盘
        app.save_device_state()
        with open(os.path.join(self.tmp.name, "devices.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("10.10.10.21", data["devices"])
        self.assertIn("10.10.10.3", data["devices"])
        self.assertEqual(data["notes"]["10.10.10.21"], "打印机")
        # ARP 里的设备一旦有流量就变成普通设备行，不再重复
        app.apply_connections_sample([conn("b", "10.10.10.21", 7, 7)], now=1005)
        snapshot = app.devices_snapshot(now=1005)
        rows = [item for item in snapshot["devices"] if item["ip"] == "10.10.10.21"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seen_via"], "traffic")
        self.assertEqual(rows[0]["mac"], "aa:aa:aa:aa:aa:21")
        self.assertEqual(snapshot["totals"]["neighbour_only"], 2)


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
        self.assertIn("设备 在线 ${data.devices.lan_online ?? data.devices.online ?? 0} / 共 ${data.devices.lan_total ?? data.devices.total ?? 0}", text)
        self.assertIn("清零统计", text)
        # 分类说明和角标
        self.assertIn("这里只统计经过本网关的连接。设备直连国内站点、或路由器做了 NAT 再转发的流量，分别不会出现或会合并显示在上级路由名下。同网段通过 ARP 看到但没有流量经过网关的设备以灰色列出。", text)
        self.assertIn("上级路由（可能汇总多台设备）", text)
        self.assertIn("远程客户端 · ${device.remote_ip_count || 0} 个来源", text)
        self.assertIn("局域网可见 · 未经网关", text)
        self.assertIn("REACHABLE: 'ARP 可达', STALE: 'ARP 过期'", text)
        self.assertIn("state-dot neighbour", text)
        self.assertIn("seen_via === 'neighbour'", text)
        self.assertIn("totals.lan_online ?? totals.online", text)
        for badge in ("'TUN'", "'SS'", "'HTTP'", "'SOCKS'", "'MIXED'"):
            self.assertIn(badge, text)

    def test_backend_classification_contract(self):
        text = APP.read_text(encoding="utf-8")
        self.assertIn('REMOTE_DEVICE_KEY = "远程客户端"', text)
        self.assertIn('run_args(["ip", "-4", "neigh"]', text)
        self.assertIn('run_args(["ip", "route"]', text)
        self.assertIn('NEIGHBOUR_VISIBLE_STATES = frozenset({"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"})', text)
        self.assertIn('"seen_via": "neighbour"', text)
        self.assertIn('"lan_online"', text)
        self.assertIn('"lan_total"', text)


if __name__ == "__main__":
    unittest.main()


class PseudoDeviceVisibilityTest(unittest.TestCase):
    def test_idle_pseudo_devices_are_hidden(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = load_app(tmp.name)
        f = app.pseudo_device_active
        self.assertFalse(f({"kind": "gateway", "active_connections": 0, "rate_up": 0, "rate_down": 0}))
        self.assertFalse(f({"kind": "remote", "active_connections": 0, "rate_up": 0, "rate_down": 0}))
        self.assertTrue(f({"kind": "remote", "active_connections": 2, "rate_up": 0, "rate_down": 0}))
        self.assertTrue(f({"kind": "gateway", "active_connections": 0, "rate_up": 0, "rate_down": 10}))
        # 普通局域网设备不受影响，离线也照常显示
        self.assertTrue(f({"kind": "lan", "active_connections": 0, "rate_up": 0, "rate_down": 0}))

