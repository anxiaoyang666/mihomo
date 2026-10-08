"""规则同步改成后台任务：保存规则的请求立即返回 sync_job，推送在线程里进行，前端轮询结果。

exec app.py（Flask 用桩、目录指到临时目录），urlopen 换成假的，检查任务生命周期、本机跳过、
对端忙（409 / 忙碌提示）重试、未启用时不建任务，以及接口在推送结束前就返回。
"""
from pathlib import Path
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from urllib import error as urlerror
from urllib import request as real_urlrequest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INDEX = APP.parent / "templates" / "index.html"
PORT = "7838"
SELF_PEER = f"http://10.10.10.9:{PORT}"
OK_PEER = f"http://10.10.20.7:{PORT}"
BUSY_PEER = "http://10.10.30.2:7838"


def load_app(tmp_dir):
    flask = types.ModuleType("flask")

    class FakeFlask:
        permanent_session_lifetime = None
        secret_key = None

        def __init__(self, *args, **kwargs):
            self.config = {}

        def route(self, *args, **kwargs):
            return lambda func: func

        def before_request(self, func):
            return func

    for name in ("render_template", "request", "jsonify", "Response", "redirect", "session"):
        setattr(flask, name, None)
    flask.Flask = FakeFlask
    sys.modules["flask"] = flask
    source = APP.read_text(encoding="utf-8").replace('MIHOMO_DIR = "/etc/mihomo"', f'MIHOMO_DIR = "{tmp_dir}"')
    module = types.ModuleType("mihomo_rule_sync_jobs_test")
    exec(compile(source, str(APP), "exec"), module.__dict__)
    return module


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_response(message="已同步规则：force-cn"):
    return FakeResponse(json.dumps({"success": True, "message": message}).encode("utf-8"))


class RuleSyncJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = self.app = load_app(self.tmp.name)
        app.local_ipv4_addresses = lambda: {"127.0.0.1", "localhost", "10.10.10.9"}
        app.SYNC_BUSY_DELAY = 0
        app.request = types.SimpleNamespace(
            host_url=SELF_PEER + "/",
            method="POST",
            path="/api/rules/force-cn",
            headers={"X-Requested-With": "XMLHttpRequest"},
            get_json=lambda silent=True: {"content": "qq.com\n"},
        )
        app.session = {"logged_in": True}
        app.jsonify = lambda payload: payload
        app.write_env({
            "WEB_PORT": PORT,
            "RULE_SYNC_ENABLED": "true",
            "RULE_SYNC_TOKEN": "sync-token-for-tests",
            "RULE_SYNC_PEERS": ",".join([SELF_PEER, OK_PEER, BUSY_PEER]),
        })
        self.calls = []
        self.busy_left = {BUSY_PEER: 2}
        self.gate = None

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            self.calls.append(url)
            if self.gate is not None:
                self.gate.wait(5)
            for peer, left in self.busy_left.items():
                if url.startswith(peer) and left > 0:
                    self.busy_left[peer] = left - 1
                    raise urlerror.HTTPError(url, 409, "CONFLICT", {}, None)
            return ok_response()

        app.urlrequest = types.SimpleNamespace(Request=real_urlrequest.Request, urlopen=fake_urlopen)

    def tearDown(self):
        if self.gate is not None:
            self.gate.set()
        self.tmp.cleanup()

    def wait_finished(self, job_id, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            job = self.app.find_sync_job(job_id)
            if job and job["finished_at"]:
                return job
            time.sleep(0.01)
        self.fail("同步任务没有结束")

    def test_job_lifecycle_skips_self_and_retries_busy_peer(self):
        job_id, message = self.app.start_broadcast("force-cn", "qq.com\n")
        self.assertTrue(job_id)
        self.assertIn("正在后台同步到 2 个节点", message)
        job = self.wait_finished(job_id)
        self.assertEqual(job["rule_id"], "force-cn")
        self.assertEqual(job["total"], 3)
        self.assertEqual(job["done"], 3)
        self.assertIsInstance(job["started_at"], int)
        self.assertGreaterEqual(job["finished_at"], job["started_at"])
        by_peer = {item["peer"]: item for item in job["results"]}
        self.assertEqual(by_peer[SELF_PEER], {"peer": SELF_PEER, "success": True, "message": "本机（跳过）"})
        self.assertTrue(by_peer[OK_PEER]["success"])
        self.assertTrue(by_peer[BUSY_PEER]["success"], "409 两次后第三次成功")
        self.assertEqual(sum(1 for url in self.calls if url.startswith(BUSY_PEER)), 3)
        self.assertFalse(any(url.startswith(SELF_PEER) for url in self.calls), "不推给自己")
        self.assertEqual(self.app.find_sync_job("latest")["id"], job_id)
        self.assertIsNone(self.app.find_sync_job("nope"))

    def test_busy_peer_gives_up_after_three_retries(self):
        self.busy_left[BUSY_PEER] = 99
        job = self.wait_finished(self.app.start_broadcast("force-cn", "qq.com\n")[0])
        failed = [item for item in job["results"] if not item["success"]]
        self.assertEqual([item["peer"] for item in failed], [BUSY_PEER])
        self.assertIn("409", failed[0]["message"])
        self.assertEqual(sum(1 for url in self.calls if url.startswith(BUSY_PEER)), 1 + self.app.SYNC_BUSY_RETRIES)

    def test_busy_message_with_http_200_is_retried(self):
        replies = ["另一个操作正在进行中，请稍后再试。", "另一个操作正在进行中，请稍后再试。"]

        def fake_urlopen(req, timeout=None):
            self.calls.append(req.full_url)
            if req.full_url.startswith(BUSY_PEER) and replies:
                return FakeResponse(json.dumps({"success": False, "message": replies.pop()}).encode("utf-8"))
            return ok_response()

        self.app.urlrequest.urlopen = fake_urlopen
        job = self.wait_finished(self.app.start_broadcast("force-cn", "qq.com\n")[0])
        self.assertTrue(all(item["success"] for item in job["results"]))
        self.assertEqual(sum(1 for url in self.calls if url.startswith(BUSY_PEER)), 3)

    def test_disabled_sync_creates_no_job(self):
        self.app.write_env({"RULE_SYNC_ENABLED": "false"})
        self.assertEqual(self.app.start_broadcast("force-cn", "qq.com"), (None, "规则同步未启用。"))
        self.app.write_env({"RULE_SYNC_ENABLED": "true", "RULE_SYNC_PEERS": ""})
        self.assertEqual(self.app.start_broadcast("force-cn", "qq.com")[0], None)
        self.assertEqual(self.app.SYNC_JOBS, [])
        self.assertEqual(self.calls, [])

    def test_only_last_five_jobs_are_kept(self):
        ids = [self.app.start_broadcast("force-cn", "qq.com")[0] for _ in range(7)]
        for job_id in ids[-5:]:
            self.wait_finished(job_id)
        self.assertEqual([job["id"] for job in self.app.SYNC_JOBS], ids[-5:])
        self.assertIsNone(self.app.find_sync_job(ids[0]))

    def test_rule_save_returns_before_broadcast_finishes(self):
        app = self.app
        app.save_rule_content = lambda rule_id, content: (True, "规则已写入 mihomo 配置")
        app.restart_mihomo = lambda: (True, "mihomo 已重启")
        self.gate = threading.Event()
        result = app.api_rules("force-cn")
        self.assertTrue(result["success"])
        job_id = result["sync_job"]
        self.assertTrue(job_id)
        self.assertIn("规则已保存并重启 mihomo；正在后台同步到 2 个节点", result["message"])
        job = app.find_sync_job(job_id)
        self.assertIsNone(job["finished_at"], "响应返回时推送还卡在假的对端上")
        self.assertEqual(app.api_rule_sync_job(job_id)["job"]["id"], job_id)
        self.gate.set()
        self.assertEqual(len(self.wait_finished(job_id)["results"]), 3)
        self.assertEqual(app.api_rule_sync_job("latest")["job"]["id"], job_id)
        missing = app.api_rule_sync_job("deadbeef")
        self.assertEqual(missing[1], 404)

    def test_rule_save_without_sync_returns_null_job(self):
        app = self.app
        app.save_rule_content = lambda rule_id, content: (True, "规则已写入 mihomo 配置")
        app.restart_mihomo = lambda: (True, "mihomo 已重启")
        app.write_env({"RULE_SYNC_ENABLED": "false"})
        result = app.api_rules("force-cn")
        self.assertTrue(result["success"])
        self.assertIsNone(result["sync_job"])
        self.assertEqual(app.api_rule_sync_job("latest"), {"success": True, "job": None})


class RuleSyncPollingContractTest(unittest.TestCase):
    def test_frontend_polls_job_and_swallows_errors(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("res.sync_job", html)
        self.assertIn("已保存，正在同步…", html)
        self.assertIn("const SYNC_POLL_INTERVAL_MS = 2000;", html)
        self.assertIn("const SYNC_POLL_DEADLINE_MS = 120000;", html)
        self.assertIn("'/api/rule-sync-jobs/' + encodeURIComponent(jobId)", html)
        fetch_fn = html[html.find("async function fetchSyncJob"): html.find("function summarizeSyncJob")]
        # 轮询不走 api()（会弹错误），任何失败都只是返回 null
        self.assertNotIn("await api(", fetch_fn)
        self.assertIn("if (!resp.ok) return null;".replace("resp", "resp"), fetch_fn)
        self.assertIn("catch (error) {", fetch_fn)
        self.assertIn("return null;", fetch_fn[fetch_fn.find("catch (error)"):])
        poll_fn = html[html.find("async function pollSyncJob"): html.find("async function loadSyncJobSummary")]
        self.assertIn("job && job.finished_at", poll_fn)
        self.assertIn("同步结果未取回，可稍后在规则同步区域查看", poll_fn)
        self.assertIn("同步完成：成功 ${succeeded} / 失败 ${failed.length} / 本机跳过 ${skipped}", html)
        self.assertIn("失败节点", html)
        self.assertIn('id="syncJobSummary"', html)
        self.assertIn("fetchSyncJob('latest')", html)
        load_fn = html[html.find("async function loadSyncSettings"):]
        self.assertIn("loadSyncJobSummary()", load_fn[:200])

    def test_backend_exposes_job_endpoint(self):
        text = APP.read_text(encoding="utf-8")
        self.assertIn('@app.route("/api/rule-sync-jobs/<job_id>")', text)
        route = text[text.find('@app.route("/api/rule-sync-jobs/<job_id>")'):]
        self.assertIn("@login_required", route[:120])
        self.assertIn("SYNC_JOBS_KEEP = 5", text)
        self.assertIn("daemon=True", text[text.find("def start_broadcast"):])
