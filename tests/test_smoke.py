#!/usr/bin/env python3
"""Smoke tests for itty-bitty-project-tracker (stdlib only, no deps).

Usage:
  python3 -m unittest tests/test_smoke.py -v
  python3 tests/test_smoke.py
"""
import concurrent.futures
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


class _ProjectFixture(unittest.TestCase):
    """Base: creates a fresh temp project dir before each test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = Path(self.tmp) / "proj"
        self.proj.mkdir()
        scripts_dir = self.proj / "scripts"
        scripts_dir.mkdir()
        shutil.copy(str(SCRIPTS / "todo.py"), str(scripts_dir / "todo.py"))
        (scripts_dir / "tracker_config.py").write_text(
            'PROJECT_TITLE = "Smoke Test"\n'
            'SECTION_ORDER = [("active", "Active"), ("backlog", "Backlog")]\n'
            'STANDING_SLUG = "backlog"\n'
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.proj / "scripts" / "todo.py")] + list(args),
            cwd=str(self.proj), capture_output=True, text=True,
        )

    def _init(self):
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)


class TestInit(_ProjectFixture):
    def test_init_creates_db(self):
        self._init()
        self.assertTrue((self.proj / "action_items.db").exists())


class TestAdd(_ProjectFixture):
    def test_add_explicit_section(self):
        self._init()
        r = self._run("add", "--section", "active", "--title", "Test task")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("#1", r.stdout)

    def test_add_default_section(self):
        """--section omitted should default to the first SECTION_ORDER slug."""
        self._init()
        r = self._run("add", "--title", "Default section task")
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._run("list")
        self.assertIn("Default section task", r2.stdout)

    def test_list_shows_added_item(self):
        self._init()
        self._run("add", "--section", "active", "--title", "Listed task")
        r = self._run("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Listed task", r.stdout)


class TestDone(_ProjectFixture):
    def test_done_removes_from_list(self):
        self._init()
        self._run("add", "--section", "active", "--title", "Task to close")
        r = self._run("done", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._run("list")
        self.assertNotIn("Task to close", r2.stdout)

    def test_done_writes_archive(self):
        self._init()
        self._run("add", "--section", "active", "--title", "Archived task")
        self._run("done", "1")
        archive = self.proj / "action_items_archive.md"
        self.assertTrue(archive.exists())
        self.assertIn("Archived task", archive.read_text())


class TestFingerprint(_ProjectFixture):
    def test_fingerprint_is_16_hex_chars(self):
        self._init()
        self._run("add", "--section", "active", "--title", "FP Test ⚠️")  # ⚠️
        db = self.proj / "action_items.db"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            "SELECT title, owner, deadline, section, status_tag, status_detail, xp_tags "
            "FROM items WHERE raw_id='1'"
        ).fetchone())
        conn.close()
        from rollup import item_fingerprint
        fp = item_fingerprint(row)
        self.assertIsInstance(fp, str)
        self.assertEqual(len(fp), 16)
        self.assertRegex(fp, r'^[0-9a-f]{16}$')


class TestConcurrency(_ProjectFixture):
    def test_concurrent_adds_no_id_collision(self):
        """Four concurrent adds should produce four distinct raw_ids."""
        self._init()

        def add_one(_):
            return subprocess.run(
                [sys.executable, str(self.proj / "scripts" / "todo.py"),
                 "add", "--section", "active", "--title", "Concurrent"],
                cwd=str(self.proj), capture_output=True, text=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(add_one, range(4)))

        for r in results:
            self.assertEqual(r.returncode, 0, r.stderr)

        r = self._run("list", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = json.loads(r.stdout)
        ids = [it["raw_id"] for it in items]
        self.assertEqual(len(ids), len(set(ids)), f"Duplicate raw_ids: {ids}")


class TestRollup(_ProjectFixture):
    def test_rollup_html_contains_xp_item(self):
        """rollup.py --html should include an [XP] item from a sub-project."""
        hub = Path(self.tmp) / "hub"
        hub_scripts = hub / "scripts"
        hub_scripts.mkdir(parents=True)
        shutil.copy(str(SCRIPTS / "todo.py"),   str(hub_scripts / "todo.py"))
        shutil.copy(str(SCRIPTS / "rollup.py"), str(hub_scripts / "rollup.py"))
        (hub_scripts / "tracker_config.py").write_text(
            f'PROJECT_TITLE = "Hub"\n'
            f'SECTION_ORDER = [("active", "Active")]\n'
            f'STANDING_SLUG = "watch"\n'
            f'PROJECTS = [("proj", "{self.proj}")]\n'
        )
        subprocess.run(
            [sys.executable, str(hub_scripts / "todo.py"), "init"],
            cwd=str(hub), capture_output=True
        )
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        self._run("add", "--section", "active",
                  "--title", "[XP] Surface me", "--deadline", soon)

        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("Surface me", html)


class TestExportGuard(_ProjectFixture):
    def test_quick_check_ok_on_fresh_db(self):
        self._init()
        conn = sqlite3.connect(self.proj / "action_items.db")
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        conn.close()

    def test_export_regenerates_md_on_healthy_db(self):
        """The quick_check->REINDEX guard must not block a normal export."""
        self._init()
        self._run("add", "--section", "active", "--title", "Exported item")
        md = self.proj / "action_items.md"
        md.unlink(missing_ok=True)
        r = self._run("export")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(md.exists())
        self.assertIn("Exported item", md.read_text())


class TestDBErrors(_ProjectFixture):
    def test_corrupt_db_gives_friendly_message(self):
        """A malformed DB yields a friendly message, not a traceback."""
        self._init()
        db = self.proj / "action_items.db"
        with open(db, "r+b") as f:
            f.seek(0)
            f.write(b"NOTSQLITEHDR\x00\x00\x00\x00")
        r = self._run("list")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("corrupt or has a cloud-sync conflict", r.stderr)

    def test_locked_db_gives_friendly_message(self):
        """A write that can't get the lock within busy_timeout yields a friendly
        message, not a traceback. Holds an EXCLUSIVE lock longer than the 5 s
        busy_timeout so the writer reliably times out."""
        self._init()
        db = self.proj / "action_items.db"
        locker = subprocess.Popen(
            [sys.executable, "-c",
             "import sqlite3,sys,time;"
             "c=sqlite3.connect(sys.argv[1]);"
             "c.execute('BEGIN EXCLUSIVE');"
             "print('locked', flush=True);"
             "time.sleep(8)", str(db)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            locker.stdout.readline()  # block until the EXCLUSIVE lock is held
            r = self._run("add", "--section", "active", "--title", "blocked")
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("busy/locked", r.stderr)
        finally:
            locker.terminate()
            locker.wait()
            locker.stdout.close()


class TestServe(_ProjectFixture):
    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _make_hub(self):
        hub = Path(self.tmp) / "hub"
        hub_scripts = hub / "scripts"
        hub_scripts.mkdir(parents=True)
        for f in ("todo.py", "rollup.py", "serve.py"):
            shutil.copy(str(SCRIPTS / f), str(hub_scripts / f))
        (hub_scripts / "tracker_config.py").write_text(
            'PROJECT_TITLE = "Hub"\n'
            'SECTION_ORDER = [("active", "Active")]\n'
            'STANDING_SLUG = "watch"\n'
            f'PROJECTS = [("proj", "{self.proj}")]\n'
        )
        subprocess.run([sys.executable, str(hub_scripts / "todo.py"), "init"],
                       cwd=str(hub), capture_output=True)
        return hub, hub_scripts

    def test_ping_authenticated_add_and_csrf_guard(self):
        hub, hub_scripts = self._make_hub()
        self._init()  # the sub-project DB
        port = self._free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [sys.executable, str(hub_scripts / "serve.py"),
             "--port", str(port), "--no-browser"],
            cwd=str(hub), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            for _ in range(50):  # wait up to ~5 s for readiness
                try:
                    with urllib.request.urlopen(base + "/api/ping", timeout=1) as resp:
                        if resp.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("server did not become ready")

            with urllib.request.urlopen(base + "/api/ping", timeout=2) as resp:
                self.assertTrue(json.load(resp)["ok"])

            body = json.dumps({"project": "proj", "section": "active",
                               "title": "Web add"}).encode()

            # Authenticated write: custom X-Tracker header present.
            req = urllib.request.Request(base + "/api/add", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Tracker", "1")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertTrue(json.load(resp).get("ok"))
            r = self._run("list", "--json")
            titles = [it["title"] for it in json.loads(r.stdout)]
            self.assertIn("Web add", titles)

            # CSRF guard: same request without X-Tracker must be rejected.
            req2 = urllib.request.Request(base + "/api/add", data=body, method="POST")
            req2.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req2, timeout=10)
            self.assertEqual(cm.exception.code, 403)
            cm.exception.close()
        finally:
            server.terminate()
            server.wait(timeout=5)
            server.stdout.close()


if __name__ == "__main__":
    unittest.main()
