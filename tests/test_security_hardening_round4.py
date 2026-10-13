"""第四轮加固：密码哈希迁移、监听地址、升级包校验、重启健康检查、内核摘要。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from test_rule_sync_render import ROOT, load_app


VERIFY = ROOT / "remote-root" / "etc" / "mihomo" / "scripts" / "verify_github_asset.py"
HEALTH = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "panel_healthcheck.py"
COMMIT = "a" * 40
TREE = "b" * 40


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_github_asset", VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PasswordAndBindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = load_app(self.tmp.name)
        for key in ("WEB_USER", "WEB_SECRET", "WEB_HOST", "WEB_COOKIE_SECURE", "GH_PROXY", "GH_PROXY_BINARIES"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plaintext_secret_is_hashed_and_still_logs_in(self):
        self.app.write_env({"WEB_USER": "ops", "WEB_SECRET": "correct-horse-9"})
        self.assertTrue(self.app.check_creds("ops", "correct-horse-9"))
        stored = self.app.read_env()["WEB_SECRET"]
        self.assertTrue(self.app.password_is_hashed(stored))
        self.assertNotIn("correct-horse-9", stored)
        self.assertEqual(self.app.read_env()["WEB_PASSWORD_STRONG"], "true")
        self.assertTrue(self.app.check_creds("ops", "correct-horse-9"))
        self.assertFalse(self.app.check_creds("ops", "wrong"))
        self.assertTrue(self.app._stdlib_check_password_hash(stored, "correct-horse-9"))
        self.assertFalse(self.app._stdlib_check_password_hash(stored, "wrong"))

        status = self.app.security_status()
        self.assertTrue(status["password_strong"])
        blob = json.dumps(status)
        self.assertNotIn("correct-horse-9", blob)
        self.assertNotIn(stored, blob)

    def test_weak_plaintext_stays_a_weak_hash(self):
        self.app.write_env({"WEB_USER": "ops", "WEB_SECRET": "s3cret"})
        self.app.ensure_web_secret_hashed()
        self.assertEqual(self.app.read_env()["WEB_PASSWORD_STRONG"], "false")
        self.assertFalse(self.app.security_status()["password_strong"])
        self.assertTrue(self.app.check_creds("ops", "s3cret"))

    def test_migration_failure_does_not_lock_the_user_out(self):
        self.app.write_env({"WEB_USER": "ops", "WEB_SECRET": "s3cret"})
        real = self.app.write_env

        def boom(updates):
            if "WEB_SECRET" in updates:
                raise OSError("disk full")
            return real(updates)

        self.app.write_env = boom
        self.assertTrue(self.app.check_creds("ops", "s3cret"))
        self.assertEqual(self.app.read_env()["WEB_SECRET"], "s3cret")
        self.assertFalse(self.app.check_creds("ops", "nope"))

    def test_bind_address_and_cookie_secure_defaults_keep_lan_http(self):
        self.assertEqual(self.app.web_bind_host({}), "0.0.0.0")
        self.assertEqual(self.app.normalize_web_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(self.app.normalize_web_host("[::1]"), "::1")
        self.assertEqual(self.app.normalize_web_host("not a host"), "0.0.0.0")
        self.assertFalse(self.app.cookie_secure_enabled({}))
        self.assertTrue(self.app.cookie_secure_enabled({"WEB_COOKIE_SECURE": "true"}))
        self.assertFalse(self.app.cookie_secure_enabled({"WEB_COOKIE_SECURE": "false"}))
        self.app.write_env({"WEB_HOST": "10.1.1.1", "WEB_COOKIE_SECURE": "true"})
        self.assertEqual(self.app.web_bind_host(), "10.1.1.1")
        self.app.apply_session_cookie_secure()
        self.assertTrue(self.app.app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(self.app.panel_probe_url(), "http://10.1.1.1:7838/login")
        self.assertIn("host=host", (ROOT / "remote-root/etc/mihomo/manager/app.py").read_text(encoding="utf-8"))

    def test_binary_proxy_urls_are_opt_in(self):
        url = "https://github.com/MetaCubeX/mihomo/releases/download/v1.2.3/mihomo-linux-amd64-compatible-v1.2.3.gz"
        self.app.write_env({"GH_PROXY": "https://mirror.example/"})
        self.assertEqual(self.app.github_binary_candidate_urls(url), [url])
        self.app.write_env({"GH_PROXY_BINARIES": "true"})
        self.assertEqual(self.app.github_binary_candidate_urls(url), [url, "https://mirror.example/" + url])
        # 源码包仍按原来的 GH_PROXY 回退，不受二进制开关影响
        self.app.write_env({"GH_PROXY_BINARIES": "false"})
        self.assertEqual(self.app.github_candidate_urls(url), [url, "https://mirror.example/" + url])


class AssetVerifyTest(unittest.TestCase):
    def setUp(self):
        self.verifier = load_verifier()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "core.gz"
        self.path.write_bytes(b"gzip-bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sha256_and_size_and_proxy_without_digest(self):
        digest = "sha256:" + hashlib.sha256(b"gzip-bytes").hexdigest()
        meta = {"size": len(b"gzip-bytes"), "digest": digest.upper()}
        ok, message = self.verifier.verify_file(self.path, meta, False)
        self.assertTrue(ok, message)
        self.assertIn("sha256", message)

        bad, message = self.verifier.verify_file(self.path, {"size": 3, "digest": digest}, False)
        self.assertFalse(bad)
        self.assertIn("大小", message)

        bad, message = self.verifier.verify_file(self.path, {"size": None, "digest": "sha256:" + "ab" * 32}, True)
        self.assertFalse(bad)
        self.assertIn("sha256", message)

        ok, message = self.verifier.verify_file(self.path, {"size": None, "digest": ""}, False)
        self.assertTrue(ok, message)
        bad, message = self.verifier.verify_file(self.path, {"size": None, "digest": ""}, True)
        self.assertFalse(bad)
        self.assertIn("代理", message)

    def test_cli_rejects_proxied_asset_without_digest(self):
        meta = Path(self.tmp.name) / "release.json"
        meta.write_text("{}", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(VERIFY), str(self.path), str(meta), "missing.gz", "1"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("sha256", result.stderr)


class PanelUpgradeIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = load_app(self.tmp.name)
        self.app.write_env({
            "MIHOMO_PANEL_REPO_URL": "https://github.com/anxiaoyang666/mihomo.git",
            "MIHOMO_PANEL_BRANCH": "main",
        })
        self.downloads = []

    def tearDown(self):
        self.tmp.cleanup()

    def _tree(self, files):
        entries = []
        for rel, data in files.items():
            entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": self.app.git_blob_sha1(data)})
        return entries

    def _zip(self, files, commit=COMMIT, repo="mihomo"):
        buffer = io.BytesIO()
        prefix = f"{repo}-{commit}/"
        with zipfile.ZipFile(buffer, "w") as archive:
            for rel, data in files.items():
                archive.writestr(prefix + rel, data)
        path = self.dir / "panel.zip"
        path.write_bytes(buffer.getvalue())
        return path

    def _install_fakes(self, files, zip_path, version_bytes):
        blob = self.app.git_blob_sha1(version_bytes)
        content = base64.b64encode(version_bytes).decode("ascii")

        def fake_read(urls, timeout=20, max_bytes=None):
            url = urls[0]
            if "/commits/" in url and "/git/" not in url:
                body = {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}
            elif "/contents/" in url:
                body = {"sha": blob, "encoding": "base64", "content": content}
            elif "/git/trees/" in url:
                body = {"sha": TREE, "truncated": False, "tree": self._tree(files)}
            else:
                return False, "unexpected " + url, ""
            return True, json.dumps(body), url

        def fake_download(urls, output, timeout=30, max_bytes=None):
            self.downloads.append(urls)
            Path(output).write_bytes(zip_path.read_bytes())
            return True, urls[0]

        self.app.read_url_text = fake_read
        self.app.download_file = fake_download

    def test_verified_commit_archive_is_accepted(self):
        app_bytes = b'PANEL_VERSION = "9.9.9"\n'
        cli_bytes = b"#!/bin/sh\n"
        files = {
            "remote-root/etc/mihomo/manager/app.py": app_bytes,
            "remote-root/usr/bin/mihomo": cli_bytes,
        }
        self._install_fakes(files, self._zip(files), app_bytes)
        work = self.dir / "work"
        work.mkdir()
        ok, source, root, settings = self.app.download_panel_source(str(work))
        self.assertTrue(ok, source)
        self.assertEqual(settings["remote_version"], "9.9.9")
        self.assertEqual(settings["commit"], COMMIT)
        self.assertTrue(source.startswith("https://github.com/"))
        self.assertTrue(os.path.isfile(os.path.join(root, "usr/bin/mihomo")))
        self.assertEqual(self.downloads[0][0], f"https://github.com/anxiaoyang666/mihomo/archive/{COMMIT}.zip")

    def test_same_version_does_not_download(self):
        app_bytes = b'PANEL_VERSION = "0.1.61"\n'
        files = {"remote-root/etc/mihomo/manager/app.py": app_bytes}
        self._install_fakes(files, self._zip(files), app_bytes)
        work = self.dir / "work"
        work.mkdir()
        ok, _source, root, settings = self.app.download_panel_source(str(work))
        self.assertTrue(ok)
        self.assertIsNone(root)
        self.assertEqual(settings["remote_version"], "0.1.61")
        self.assertEqual(self.downloads, [])

    def test_tampered_archive_is_refused(self):
        app_bytes = b'PANEL_VERSION = "9.9.9"\n'
        files = {
            "remote-root/etc/mihomo/manager/app.py": app_bytes,
            "remote-root/usr/bin/mihomo": b"#!/bin/sh\n",
        }
        tampered = dict(files)
        tampered["remote-root/etc/mihomo/manager/app.py"] = b'PANEL_VERSION = "9.9.9"\n# tampered\n'
        self._install_fakes(files, self._zip(tampered), app_bytes)
        work = self.dir / "work"
        work.mkdir()
        ok, message, root, _settings = self.app.download_panel_source(str(work))
        self.assertFalse(ok)
        self.assertIsNone(root)
        self.assertIn("取消升级", message)

    def test_api_failure_refuses_unverified_upgrade(self):
        self.app.read_url_text = lambda urls, timeout=20, max_bytes=None: (False, "timed out", "")
        work = self.dir / "work"
        work.mkdir()
        ok, message, root, _settings = self.app.download_panel_source(str(work))
        self.assertFalse(ok)
        self.assertIsNone(root)
        self.assertIn("未经校验", message)

    def test_symlink_in_zip_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo(f"mihomo-{COMMIT}/remote-root/link")
            info.external_attr = (0o120777) << 16
            archive.writestr(info, "anywhere")
        path = self.dir / "bad.zip"
        path.write_bytes(buffer.getvalue())
        ok, message = self.app.zip_names_match_commit(str(path), "mihomo", COMMIT)
        self.assertFalse(ok)
        self.assertIn("符号链接", message)


class HealthcheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.target = self.dir / "manager"
        self.target.mkdir()
        (self.target / "app.py").write_text("new\n", encoding="utf-8")
        self.backup = self.dir / "backup"
        stored = self.backup / str(self.target).lstrip("/")
        stored.mkdir(parents=True)
        (stored / "app.py").write_text("old\n", encoding="utf-8")
        manifest = [{"target": str(self.target), "kind": "dir", "existed": True}]
        (self.backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.evil = self.dir / "evil.txt"
        self.evil.write_text("keep\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, **overrides):
        plan = {
            "backup_root": str(self.backup),
            "allowed_targets": [str(self.target)],
            "old_pid": 0,
            "url": "http://127.0.0.1:1/login",
            "control_service": False,
            "attempts": 1,
            "interval": 0,
            "initial_wait": 0,
            "pid_wait": 0,
            "pid_interval": 0.05,
            "armed_path": str(self.dir / "armed"),
            "log_path": str(self.dir / "health.log"),
        }
        plan.update(overrides)
        path = self.dir / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _run(self, plan_path):
        return subprocess.run([sys.executable, str(HEALTH), str(plan_path)], capture_output=True, text=True)

    def test_failed_login_restores_backup_and_ignores_other_paths(self):
        manifest = json.loads((self.backup / "manifest.json").read_text(encoding="utf-8"))
        manifest.append({"target": str(self.evil), "kind": "file", "existed": False})
        (self.backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = self._run(self._plan())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.target / "app.py").read_text(encoding="utf-8"), "old\n")
        self.assertTrue(self.evil.exists())
        self.assertEqual(self.evil.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue((self.dir / "armed").exists())
        self.assertEqual(stat.S_IMODE((self.dir / "armed").stat().st_mode) & 0o077, 0)

    def test_healthy_login_keeps_the_new_files(self):
        body = b'<form action="/login"><input name="password" type="password"></form>'

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/login"
            result = self._run(self._plan(url=url, attempts=3, interval=0))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "app.py").read_text(encoding="utf-8"), "new\n")
        self.assertIn("保留新版本", result.stderr)


if __name__ == "__main__":
    unittest.main()
