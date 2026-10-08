"""爱快数据源：用假的 ikuai_http_get 喂合成的爱快响应，检查信封解析、分页、设备行合成、设置校验和应用统计。

沿用 test_devices_functional.py 的方式 exec app.py（Flask 用桩、MIHOMO_DIR 指到临时目录）。
所有 MAC / IP 都是编的，不是真实设备。
"""
from pathlib import Path
import os
import tempfile
import unittest

from test_devices_functional import APP, INDEX, conn, load_app


ROUTER = "10.10.10.253"
GATEWAY = "10.10.10.5"
GATEWAY_MAC = "02:00:00:00:00:05"


def client(ip, mac, **fields):
    row = {
        "ip_addr": ip, "mac": mac, "comment": "", "hostname": "", "termname": "", "client_type": "Unknown",
        "client_vendor": "Unknown", "client_model": "", "device_type": "", "upload": 0, "download": 0,
        "uprate": "", "downrate": "", "total_up": 0, "total_down": 0, "today_total": 0, "connect_num": 0,
        "uptime": "2026-10-08 09:00:00", "timestamp": 1000, "interface": "lan1", "ssid": "", "signal": 0,
    }
    row.update(fields)
    return row


def ok(results):
    return {"code": 0, "message": "Success", "results": results}


class FakeIkuai:
    """按路径返回合成响应，记录每次调用。"""

    def __init__(self, online=None, offline=None, static=None, apps=None):
        self.online = online or []
        self.offline = offline or []
        self.static = static or []
        self.apps = apps or []
        self.calls = []
        self.fail = None

    def __call__(self, base_url, token, path, params=None, timeout=5):
        self.calls.append((base_url, token, path, dict(params or {})))
        if self.fail:
            raise RuntimeError(self.fail)
        page = int((params or {}).get("page", 1))
        limit = int((params or {}).get("limit", 100))
        chunk = lambda items: items[(page - 1) * limit: page * limit]
        if path.endswith("/clients-online"):
            return ok({"data": chunk(self.online), "total": len(self.online)})
        if path.endswith("/clients-offline"):
            return ok({"offline_data": chunk(self.offline), "offline_total": len(self.offline)})
        if path.endswith("/dhcp/static"):
            return ok({"static_data": chunk(self.static), "static_total": len(self.static)})
        if path.endswith("/app-protocols/load"):
            return ok({"data": self.apps})
        raise AssertionError("unexpected path " + path)


class IkuaiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        self.fake = FakeIkuai()
        self.app.ikuai_http_get = self.fake

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, url="https://" + ROUTER, token="tok-123"):
        self.app.write_env({"IKUAI_URL": url, "IKUAI_TOKEN": token})

    def env_text(self):
        path = os.path.join(self.tmp.name, ".env")
        return Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""


class EnvelopeAndPagingTest(IkuaiTestBase):
    def test_envelope_data_results_and_errors(self):
        unwrap = self.app.ikuai_unwrap
        self.assertEqual(unwrap({"code": 0, "data": {"a": 1}, "results": {"b": 2}}), (True, {"a": 1}))
        self.assertEqual(unwrap({"code": 0, "data": None, "results": {"b": 2}}), (True, {"b": 2}))
        self.assertEqual(unwrap({"code": 20000, "message": "ok", "results": [1]}), (True, [1]))
        self.assertEqual(unwrap({"code": "0", "results": []}), (True, []))
        ok_, message = unwrap({"code": 3007, "message": "token 无效"})
        self.assertFalse(ok_)
        self.assertIn("token 无效", message)
        self.assertFalse(unwrap({"message": "no code"})[0])
        self.assertFalse(unwrap(["not", "a", "dict"])[0])

    def test_call_turns_http_failure_into_error(self):
        self.fake.fail = "HTTP 401：unauthorized"
        ok_, message = self.app.ikuai_call("https://" + ROUTER, "t", self.app.IKUAI_ONLINE_PATH)
        self.assertFalse(ok_)
        self.assertIn("HTTP 401", message)

    def test_pagination_fetches_all_pages_and_caps(self):
        app = self.app
        self.fake.online = [client(f"10.1.{i // 250}.{i % 250 + 1}", f"02:00:00:00:{i // 256:02x}:{i % 256:02x}") for i in range(130)]
        ok_, items = app.ikuai_fetch_pages("https://" + ROUTER, "t", app.IKUAI_ONLINE_PATH, "data", "total")
        self.assertTrue(ok_)
        self.assertEqual(len(items), 130)
        pages = [call[3]["page"] for call in self.fake.calls]
        self.assertEqual(pages, [1, 2])
        self.assertTrue(all(call[3]["limit"] == 100 for call in self.fake.calls))
        self.assertTrue(all(call[1] == "t" for call in self.fake.calls))

        # 总数一直说还有：最多拉 IKUAI_MAX_PAGES 页
        self.fake.calls.clear()
        endless = lambda base, token, path, params=None, timeout=5: ok({"data": [client("10.0.0.9", "02:00:00:00:00:09")] * 100, "total": 99999})
        app.ikuai_http_get = endless
        ok_, items = app.ikuai_fetch_pages("https://" + ROUTER, "t", app.IKUAI_ONLINE_PATH, "data", "total")
        self.assertTrue(ok_)
        self.assertEqual(len(items), 100 * app.IKUAI_MAX_PAGES)


class IkuaiSnapshotTest(IkuaiTestBase):
    def seed(self):
        app = self.app
        self.configure()
        self.fake.online = [
            client("10.10.10.20", "02:00:00:00:00:20", comment="客厅电视", termname="tv", upload=300, download=4000,
                   total_up=1000, total_down=9000, today_total=500, connect_num=12, client_vendor="Xiaomi"),
            client("10.10.10.21", "02:00:00:00:00:21", termname="nas-term", upload=10, download=20, today_total=900),
            client("10.10.10.22", "02:00:00:00:00:22", hostname="ZXSLC%20SR7420-ABC", today_total=50,
                   ssid="Home-5G", signal=-52, client_model="iPhone", client_vendor="Apple"),
            client("10.10.10.23", "02:00:00:00:00:23"),
            client(GATEWAY, GATEWAY_MAC, comment="mihomo", upload=50, download=50),
            client("198.18.0.51", GATEWAY_MAC, comment="fake"),
            client(ROUTER, "02:00:00:00:02:53"),
        ]
        self.fake.offline = [
            client("10.10.10.40", "02:00:00:00:00:40", termname="old-phone", logout_time=900, today_total=7),
            client("10.10.10.41", "02:00:00:00:00:20"),        # 同 MAC 已在线，去重
            client("10.10.10.21", "02:00:00:00:00:99"),        # 同 IP 已在线，去重
            client("198.18.0.77", "02:00:00:00:00:77"),        # fake-ip 段，跳过
        ]
        self.fake.static = [
            {"ip_addr": "10.10.10.21", "mac": "02:00:00:00:00:21", "tagname": "书房 NAS", "termname": "", "hostname": "", "comment": "", "enabled": "yes"},
            {"ip_addr": "10.10.10.23", "mac": "02:00:00:00:00:23", "tagname": "7", "termname": "绑定终端名", "hostname": "", "comment": "", "enabled": "yes"},
        ]
        with app.DEVICE_LOCK:
            app.DEVICE_STATE["local_ips"] = {"127.0.0.1", "::1", GATEWAY}
            app.DEVICE_STATE["upstream_router"] = ROUTER
        app.apply_connections_sample([
            conn("r1", ROUTER, 100, 5000, host="youtube.com", chains=["节点A", "PROXY"]),
            conn("r2", ROUTER, 10, 50, host="google.com", chains=["节点A", "PROXY"]),
            conn("d1", "10.10.10.22", 7, 70, host="telegram.org", chains=["节点B", "TG"]),
            conn("p1", "122.6.190.2", 1, 1, host="remote.example"),
            conn("g1", "127.0.0.1", 1, 1, host="dns.example"),
            conn("x1", "10.20.0.9", 3, 3, host="other-subnet.example"),
        ], now=1000)
        self.assertTrue(app.poll_ikuai(now=1000))

    def test_rows_names_dedupe_roles_and_attach(self):
        app = self.app
        self.seed()
        snap = app.devices_snapshot(now=1000)
        self.assertEqual(snap["data_source"], "ikuai")
        self.assertTrue(snap["ikuai"]["configured"])
        self.assertTrue(snap["ikuai"]["ok"])
        self.assertEqual(snap["ikuai"]["fetched_at"], 1000)
        ikuai_rows = [row for row in snap["devices"] if row.get("source") == "ikuai"]
        by_ip = {row["ip"]: row for row in ikuai_rows}
        self.assertEqual(sorted(by_ip), sorted(["10.10.10.20", "10.10.10.21", "10.10.10.22", "10.10.10.23", GATEWAY, ROUTER, "10.10.10.40"]))
        self.assertFalse(any(row["ip"].startswith("198.18.") for row in snap["devices"]))

        # 名字优先级
        self.assertEqual((by_ip["10.10.10.20"]["name"], by_ip["10.10.10.20"]["name_source"]), ("客厅电视", "comment"))
        self.assertEqual((by_ip["10.10.10.21"]["name"], by_ip["10.10.10.21"]["name_source"]), ("书房 NAS", "dhcp_tag"))
        self.assertEqual((by_ip["10.10.10.22"]["name"], by_ip["10.10.10.22"]["name_source"]), ("ZXSLC SR7420-ABC", "hostname"))
        self.assertEqual((by_ip["10.10.10.23"]["name"], by_ip["10.10.10.23"]["name_source"]), ("绑定终端名", "termname"))
        self.assertEqual((by_ip[ROUTER]["name"], by_ip[ROUTER]["name_source"]), ("", ""))

        tv = by_ip["10.10.10.20"]
        self.assertTrue(tv["online"])
        self.assertEqual((tv["rate_up"], tv["rate_down"]), (300, 4000))
        self.assertEqual((tv["total_up"], tv["total_down"], tv["today_total"], tv["connections"]), (1000, 9000, 500, 12))
        self.assertEqual(tv["since"], "2026-10-08 09:00:00")
        self.assertEqual(tv["vendor"], "Xiaomi")
        self.assertEqual(tv["type"], "")      # "Unknown" 当空
        self.assertNotIn("wireless", tv)
        self.assertNotIn("proxy", tv)
        self.assertEqual(by_ip["10.10.10.22"]["wireless"], {"ssid": "Home-5G", "signal": -52})

        old = by_ip["10.10.10.40"]
        self.assertFalse(old["online"])
        self.assertEqual((old["rate_up"], old["rate_down"], old["offline_at"]), (0, 0, 900))
        self.assertEqual(by_ip["10.10.10.21"]["mac"], "02:00:00:00:00:21")

        # 角色
        self.assertEqual(by_ip[GATEWAY]["role"], "proxy_gateway")
        self.assertEqual(by_ip[ROUTER]["role"], "router")
        self.assertEqual(tv["role"], "")

        # 直连 mihomo 的设备附上代理明细
        proxy = by_ip["10.10.10.22"]["proxy"]
        self.assertEqual((proxy["upload"], proxy["download"]), (7, 70))
        self.assertEqual(proxy["top_domains"][0]["host"], "telegram.org")
        self.assertEqual(proxy["chains"][0]["label"], "TG → 节点B")

        # 经爱快转发的代理流量是汇总卡片，不是设备行；爱快自己的那一行没有 proxy
        via = snap["proxy_via_router"]
        self.assertEqual(via["ip"], ROUTER)
        self.assertEqual((via["upload_total"], via["download_total"], via["active_connections"]), (110, 5050, 2))
        self.assertEqual([item["host"] for item in via["top_domains"]], ["youtube.com", "google.com"])
        self.assertEqual(via["chains"][0]["label"], "PROXY → 节点A")
        self.assertNotIn("proxy", by_ip[ROUTER])

        # 远程客户端 / 网关伪设备 / 爱快不认识但在线的直连来源保留为 mihomo 行
        extra = {row["ip"]: row for row in snap["devices"] if row.get("source") == "mihomo"}
        self.assertEqual(sorted(extra), sorted([app.REMOTE_DEVICE_KEY, app.GATEWAY_DEVICE_KEY, "10.20.0.9"]))
        self.assertNotIn(ROUTER, extra)

        # 排序：在线优先，再按总速率
        self.assertEqual(ikuai_rows[0]["ip"], "10.10.10.20")
        self.assertEqual(ikuai_rows[-1]["ip"], "10.10.10.40")

        totals = snap["totals"]
        self.assertEqual((totals["lan_total"], totals["lan_online"]), (7, 6))
        self.assertEqual(totals["rate_down"], 4000 + 20 + 50)
        self.assertEqual(app.devices_summary(now=1000), {"online": 6, "total": 7, "lan_online": 6, "lan_total": 7})

    def test_panel_note_wins_name_priority(self):
        app = self.app
        self.seed()
        app.set_device_note("10.10.10.20", "我的电视")
        row = next(row for row in app.devices_snapshot(now=1000)["devices"] if row["ip"] == "10.10.10.20")
        self.assertEqual((row["name"], row["name_source"], row["note"]), ("我的电视", "note", "我的电视"))

    def test_neighbour_rows_dropped_in_ikuai_mode(self):
        app = self.app
        self.seed()
        with app.DEVICE_LOCK:
            app.DEVICE_STATE["neighbours"] = {"10.10.10.99": {"mac": "02:00:00:00:00:99", "state": "REACHABLE"}}
        snap = app.devices_snapshot(now=1000)
        self.assertNotIn("10.10.10.99", [row["ip"] for row in snap["devices"]])
        self.assertEqual(snap["totals"]["neighbour_only"], 0)

    def test_failure_keeps_last_data_and_records_error(self):
        app = self.app
        self.seed()
        self.fake.fail = "timed out"
        app.poll_ikuai(now=1010)
        snap = app.devices_snapshot(now=1010)
        self.assertEqual(snap["data_source"], "ikuai")
        self.assertFalse(snap["ikuai"]["ok"])
        self.assertIn("timed out", snap["ikuai"]["error"])
        self.assertEqual(snap["ikuai"]["fetched_at"], 1000)
        self.assertIn("10.10.10.20", [row["ip"] for row in snap["devices"]])
        # 恢复后错误清掉
        self.fake.fail = None
        app.poll_ikuai(now=1020)
        self.assertTrue(app.devices_snapshot(now=1020)["ikuai"]["ok"])

    def test_poll_intervals(self):
        app = self.app
        self.seed()
        self.fake.calls.clear()
        app.poll_ikuai(now=1005)                 # 还没到 10 秒
        self.assertEqual(self.fake.calls, [])
        app.poll_ikuai(now=1010)                 # 只拉在线终端
        self.assertEqual({call[2] for call in self.fake.calls}, {app.IKUAI_ONLINE_PATH})
        self.fake.calls.clear()
        app.poll_ikuai(now=1300)                 # 300 秒后连 DHCP 和离线一起
        self.assertEqual({call[2] for call in self.fake.calls}, {app.IKUAI_ONLINE_PATH, app.IKUAI_STATIC_PATH, app.IKUAI_OFFLINE_PATH})
        self.assertTrue(all(call[0] == "https://" + ROUTER and call[1] == "tok-123" for call in self.fake.calls))

    def test_falls_back_to_mihomo_when_not_configured_or_never_ok(self):
        app = self.app
        app.apply_connections_sample([conn("a", "10.10.10.3", 1, 1)], now=1000)
        snap = app.devices_snapshot(now=1000)
        self.assertEqual(snap["data_source"], "mihomo")
        self.assertFalse(snap["ikuai"]["configured"])
        self.assertIsNone(snap["proxy_via_router"])
        self.assertEqual([row["ip"] for row in snap["devices"]], ["10.10.10.3"])
        self.assertEqual(snap["devices"][0]["seen_via"], "traffic")
        self.assertFalse(app.poll_ikuai(now=1000))
        self.assertEqual(self.fake.calls, [])
        # 配置了但从没成功过：仍是 mihomo 视图，带上错误
        self.configure()
        self.fake.fail = "connection refused"
        app.poll_ikuai(now=1000)
        snap = app.devices_snapshot(now=1000)
        self.assertEqual(snap["data_source"], "mihomo")
        self.assertTrue(snap["ikuai"]["configured"])
        self.assertIn("connection refused", snap["ikuai"]["error"])
        self.assertEqual(app.devices_summary(now=1000)["lan_total"], 1)


class IkuaiSettingsTest(IkuaiTestBase):
    def test_url_validation(self):
        validate = self.app.validate_ikuai_url
        self.assertEqual(validate("https://10.10.10.253/"), (True, "https://10.10.10.253"))
        self.assertEqual(validate("http://192.168.1.1:8080"), (True, "http://192.168.1.1:8080"))
        self.assertEqual(validate("https://127.0.0.1"), (True, "https://127.0.0.1"))
        self.assertEqual(validate("https://ikuai.lan"), (True, "https://ikuai.lan"))
        for bad in ("https://8.8.8.8", "ftp://10.0.0.1", "10.0.0.1", "https://10.0.0.1/api", "https://user:pw@10.0.0.1",
                    "https://bad host", "https://10.0.0.1?x=1", "", "https://"):
            self.assertFalse(validate(bad)[0], bad)

    def test_save_keeps_token_and_never_returns_it(self):
        app = self.app
        ok_, message = app.save_ikuai_settings({"url": "https://8.8.8.8", "token": "abc"})
        self.assertFalse(ok_)
        self.assertNotIn("IKUAI", self.env_text())

        ok_, _ = app.save_ikuai_settings({"url": "https://10.10.10.253/", "token": "secret-token-1"})
        self.assertTrue(ok_)
        self.assertEqual(app.read_env()["IKUAI_URL"], "https://10.10.10.253")
        self.assertEqual(app.read_env()["IKUAI_TOKEN"], "secret-token-1")
        self.assertEqual(os.stat(os.path.join(self.tmp.name, ".env")).st_mode & 0o777, 0o600)

        # 空 token 保持原值
        ok_, _ = app.save_ikuai_settings({"url": "https://10.10.10.254", "token": ""})
        self.assertTrue(ok_)
        self.assertEqual(app.read_env()["IKUAI_TOKEN"], "secret-token-1")
        self.assertEqual(app.read_env()["IKUAI_URL"], "https://10.10.10.254")

        payload = app.ikuai_settings_payload()
        self.assertEqual(payload["url"], "https://10.10.10.254")
        self.assertTrue(payload["token_set"])
        self.assertTrue(payload["configured"])
        self.assertNotIn("secret-token-1", repr(payload))
        self.assertNotIn("token", payload)

        self.assertFalse(app.save_ikuai_settings({"url": "https://10.10.10.254", "token": "has space"})[0])

        ok_, _ = app.save_ikuai_settings({"url": "https://10.10.10.254", "clear_token": True})
        self.assertTrue(ok_)
        self.assertEqual(app.read_env()["IKUAI_TOKEN"], "")
        self.assertFalse(app.ikuai_settings_payload()["token_set"])
        self.assertFalse(app.ikuai_settings_payload()["configured"])

    def test_changing_settings_drops_old_router_data(self):
        app = self.app
        self.configure()
        self.fake.online = [client("10.10.10.20", "02:00:00:00:00:20")]
        app.poll_ikuai(now=1000)
        self.assertEqual(app.devices_snapshot(now=1000)["data_source"], "ikuai")
        app.save_ikuai_settings({"url": "https://10.10.10.250", "token": ""})
        self.assertEqual(app.IKUAI_STATE["online"], [])
        self.assertEqual(app.devices_snapshot(now=1000)["data_source"], "mihomo")

    def test_connection_test_does_not_save(self):
        app = self.app
        self.fake.online = [client("10.10.10.20", "02:00:00:00:00:20"), client("10.10.10.21", "02:00:00:00:00:21")]
        initial = self.env_text()
        ok_, message = app.test_ikuai_connection({"url": "https://10.10.10.253", "token": "try-me"})
        self.assertTrue(ok_)
        self.assertEqual(message, "连接正常，在线终端 2 台")
        self.assertEqual(self.fake.calls[0][:3], ("https://10.10.10.253", "try-me", app.IKUAI_ONLINE_PATH))
        self.assertEqual(self.fake.calls[0][3]["page"], 1)
        self.assertEqual(self.env_text(), initial)
        self.assertNotIn("IKUAI", initial)
        self.assertEqual(app.IKUAI_STATE["fetched_at"], 0)

        # 空字段回落到已保存的值
        self.configure(token="saved-token")
        before = self.env_text()
        self.fake.calls.clear()
        ok_, _ = app.test_ikuai_connection({"url": "https://10.10.10.252", "token": ""})
        self.assertTrue(ok_)
        self.assertEqual(self.fake.calls[0][:2], ("https://10.10.10.252", "saved-token"))
        self.assertEqual(self.env_text(), before)

        self.assertFalse(app.test_ikuai_connection({"url": "https://1.1.1.1", "token": "x"})[0])
        self.fake.fail = "HTTP 401"
        ok_, message = app.test_ikuai_connection({})
        self.assertFalse(ok_)
        self.assertIn("HTTP 401", message)


class IkuaiAppsTest(IkuaiTestBase):
    def test_apps_top8_cache_and_unknown(self):
        app = self.app
        payload, status = app.device_apps("10.10.10.20", now=1000)
        self.assertEqual(status, 400)
        self.configure()
        self.fake.online = [client("10.10.10.20", "02:00:00:00:00:20")]
        self.fake.apps = [{"appid": i, "appname": f"app{i}", "conn_cnt": i, "upload": i, "download": 2 * i,
                           "total_up": i * 10, "total_down": i * 20, "total": i * 30} for i in range(1, 13)]
        app.poll_ikuai(now=1000)
        payload, status = app.device_apps("10.10.10.20", now=1000)
        self.assertEqual(status, 200)
        apps = payload["apps"]
        self.assertEqual(len(apps), 8)
        self.assertEqual([item["appname"] for item in apps], [f"app{i}" for i in range(12, 4, -1)])
        self.assertEqual(apps[0], {"appname": "app12", "total": 360, "total_up": 120, "total_down": 240,
                                   "rate_up": 12, "rate_down": 24, "connections": 12})
        call = [c for c in self.fake.calls if c[2] == app.IKUAI_APPS_PATH][0]
        self.assertEqual(call[3], {"ip": "10.10.10.20", "mac": "02:00:00:00:00:20", "limit": 20})

        # 30 秒内走缓存
        count = len(self.fake.calls)
        payload, _ = app.device_apps("10.10.10.20", now=1020)
        self.assertTrue(payload["cached"])
        self.assertEqual(len(self.fake.calls), count)
        app.device_apps("10.10.10.20", now=1031)
        self.assertEqual(len(self.fake.calls), count + 1)

        payload, status = app.device_apps("10.10.10.99", now=1000)
        self.assertEqual(status, 404)
        self.assertFalse(payload["success"])
        self.assertEqual(app.device_apps("not-an-ip", now=1000)[1], 400)


class IkuaiContractTest(unittest.TestCase):
    def test_routes_require_login(self):
        text = APP.read_text(encoding="utf-8")
        for route in ("@app.route('/api/devices/<ip>/apps')", "@app.route('/api/ikuai-settings', methods=['GET', 'POST'])",
                      "@app.route('/api/ikuai-test', methods=['POST'])"):
            self.assertIn(route, text)
            after = text[text.find(route):text.find(route) + 200]
            self.assertIn("@login_required", after)
        self.assertIn("IKUAI_POLL_INTERVAL = 10", text)
        self.assertIn('FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")', text)
        # 只对爱快地址关闭证书校验
        self.assertEqual(text.count("ssl.CERT_NONE"), 1)
        self.assertIn('"Authorization": "Bearer " + token', text)

    def test_ui_has_settings_card_source_label_and_lazy_apps(self):
        text = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="ikuaiCard"', text)
        self.assertIn("爱快数据源", text)
        self.assertIn('id="ikuaiUrl"', text)
        self.assertIn('placeholder="https://<上级路由 IP>"', text)
        self.assertIn('id="ikuaiToken" type="password" placeholder="留空则保持不变"', text)
        self.assertIn("在爱快「系统设置」里申请 API Token（建议只读）。面板只读取终端列表、DHCP 绑定和应用统计，不会修改路由器配置。", text)
        for label in ("测试连接", "清除 Token", "已设置"):
            self.assertIn(label, text)
        self.assertIn("api('/ikuai-test'", text)
        self.assertIn("api('/ikuai-settings'", text)
        self.assertIn('id="deviceDataSource"', text)
        self.assertIn("数据来源：<strong>爱快 ", text)
        self.assertIn("数据来源：<strong>mihomo 连接（未配置爱快）</strong>", text)
        self.assertIn("经爱快转发的代理流量（全家合计）", text)
        self.assertIn("proxy_via_router", text)
        self.assertIn("/api/devices/${encodeURIComponent(ip)}/apps", text)
        self.assertIn("加载中…", text)
        self.assertIn("DEVICE_APPS_CACHE_MS = 30000", text)
        self.assertIn("event.target.open && !wasOpen", text)
        self.assertIn("代理网关", text)
        self.assertIn("路由器", text)


if __name__ == "__main__":
    unittest.main()
