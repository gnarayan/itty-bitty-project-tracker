#!/usr/bin/env python3
"""Smoke tests for itty-bitty-project-tracker (stdlib only, no deps).

Usage:
  python3 -m unittest tests/test_smoke.py -v
  python3 tests/test_smoke.py
"""
import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
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

    def _add(self, *args):
        """Run add and return the generated hash id."""
        r = self._run("add", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"#(\S+)", r.stdout)
        self.assertIsNotNone(m, f"no id in add output: {r.stdout}")
        return m.group(1)


class TestInit(_ProjectFixture):
    def test_init_creates_db(self):
        self._init()
        self.assertTrue((self.proj / "action_items.db").exists())


class TestAdd(_ProjectFixture):
    def test_add_explicit_section(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Test task")
        # hash id: lowercase hex, at least one letter (never collides with legacy numeric ids)
        self.assertRegex(iid, r"^[0-9a-f]{4,}$")
        self.assertTrue(any(c.isalpha() for c in iid), iid)

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
        iid = self._add("--section", "active", "--title", "Task to close")
        r = self._run("done", iid)
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._run("list")
        self.assertNotIn("Task to close", r2.stdout)

    def test_done_writes_archive(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Archived task")
        self._run("done", iid)
        archive = self.proj / "action_items_archive.md"
        self.assertTrue(archive.exists())
        self.assertIn("Archived task", archive.read_text())


class TestUpdateDeadline(_ProjectFixture):
    """A bare --status update must not silently wipe an existing deadline."""

    def _deadline_of(self, raw_id):
        r = self._run("list", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = {it["raw_id"]: it for it in json.loads(r.stdout)}
        self.assertIn(raw_id, items)
        return items[raw_id]["deadline"]

    def test_status_update_preserves_existing_deadline(self):
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        iid = self._add("--section", "active", "--title", "Has a deadline",
                        "--deadline", soon)
        self.assertEqual(self._deadline_of(iid), soon)
        r = self._run("update", iid, "--status", "progress, no iso date in this text")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._deadline_of(iid), soon)

    def test_status_update_sets_deadline_from_keyword(self):
        self._init()
        iid = self._add("--section", "active", "--title", "No deadline yet")
        self.assertIsNone(self._deadline_of(iid))
        soon = (date.today() + timedelta(days=3)).isoformat()
        r = self._run("update", iid, "--status", f"deadline: {soon}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._deadline_of(iid), soon)

    def test_status_log_timestamps_are_not_mistaken_for_deadline(self):
        """Bare '**YYYY-MM-DD:** note' log-entry headers (this tracker's own
        status-log convention, also injected by `append`) must never be
        misread as a deadline."""
        self._init()
        iid = self._add("--section", "active", "--title", "No real deadline")
        self.assertIsNone(self._deadline_of(iid))
        r = self._run("update", iid, "--status",
                       "OPEN — status.\n**2026-06-30:** Abstract submitted.\n"
                       "**2026-07-07:** doc shared.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self._deadline_of(iid))

    def test_status_bold_callout_without_keyword_still_sets_deadline(self):
        """A genuine bolded deadline callout with no literal 'deadline'/'due'
        keyword should still be caught (the fallback isn't fully disabled)."""
        self._init()
        iid = self._add("--section", "active", "--title", "Hard cutoff")
        self.assertIsNone(self._deadline_of(iid))
        soon = (date.today() + timedelta(days=3)).isoformat()
        r = self._run("update", iid, "--status",
                       f"**Hard cutoff {soon} — no exceptions**")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._deadline_of(iid), soon)

    def test_status_with_explicit_deadline_flag_wins(self):
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        later = (date.today() + timedelta(days=10)).isoformat()
        iid = self._add("--section", "active", "--title", "Reschedule me",
                        "--deadline", soon)
        r = self._run("update", iid, "--status", "rescheduled", "--deadline", later)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._deadline_of(iid), later)


class TestFingerprint(_ProjectFixture):
    def test_fingerprint_is_16_hex_chars(self):
        self._init()
        self._run("add", "--section", "active", "--title", "FP Test ⚠️")  # ⚠️
        db = self.proj / "action_items.db"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            "SELECT title, owner, deadline, section, status_tag, status_detail, xp_tags "
            "FROM items"
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


class TestRegenOnRead(_ProjectFixture):
    """Unit tests for the regen-on-read staleness helpers in serve.py."""

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

    def test_stale_detection_and_regen(self):
        """_dashboard_is_stale() detects a newer DB; _ensure_fresh() rebuilds."""
        hub, hub_scripts = self._make_hub()
        self._init()  # sub-project DB

        # Build the initial dashboard.html
        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        dashboard = hub / "dashboard.html"
        self.assertTrue(dashboard.exists())

        import serve
        import rollup as rollup_mod

        hub_db    = hub / "action_items.db"
        proj_db   = self.proj / "action_items.db"
        rollup_py = hub_scripts / "rollup.py"

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                unittest.mock.patch.object(serve, "HTML_PATH", dashboard))
            stack.enter_context(
                unittest.mock.patch.object(serve, "BASE", hub))
            stack.enter_context(
                unittest.mock.patch.object(serve, "ROLLUP_PY", rollup_py))
            stack.enter_context(
                unittest.mock.patch.object(rollup_mod, "DB_PATH", hub_db))
            stack.enter_context(
                unittest.mock.patch.object(rollup_mod, "PROJECTS",
                                           [("proj", str(self.proj))]))

            # 1. Fresh render → not stale
            self.assertFalse(serve._dashboard_is_stale(),
                             "dashboard should not be stale right after rollup")

            # 2. Backdate dashboard.html (2 s ago) and set DB mtime to 1 s ago
            # so DB is newer than dashboard but both are in the past —
            # avoids relying on the system clock advancing past an artificial future mtime.
            now = time.time()
            os.utime(dashboard, (now - 2.0, now - 2.0))
            os.utime(proj_db,   (now - 1.0, now - 1.0))
            self.assertTrue(serve._dashboard_is_stale(),
                            "dashboard should be stale when DB is newer")

            # 3. _ensure_fresh() rebuilds; not stale anymore
            serve._ensure_fresh()
            self.assertFalse(serve._dashboard_is_stale(),
                             "dashboard should not be stale after _ensure_fresh()")
            self.assertTrue(dashboard.exists(), "dashboard.html must exist after regen")


class TestRecurrence(_ProjectFixture):
    """Tests for --recur flag and respawn-on-done behaviour."""

    def test_recur_add_succeeds(self):
        self._init()
        soon = (date.today() + timedelta(days=5)).isoformat()
        r = self._run("add", "--section", "active",
                      "--title", "Monthly standup",
                      "--deadline", soon, "--recur", "monthly")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"Added item #[0-9a-f]+")

    def test_recur_without_deadline_fails_cleanly(self):
        self._init()
        r = self._run("add", "--section", "active",
                      "--title", "No deadline", "--recur", "monthly")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("deadline", r.stderr)

    def test_invalid_recur_rule_fails_cleanly(self):
        self._init()
        soon = (date.today() + timedelta(days=5)).isoformat()
        r = self._run("add", "--section", "active",
                      "--title", "Bad rule",
                      "--deadline", soon, "--recur", "biweekly")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)

    def test_done_respawns_next_occurrence(self):
        self._init()
        # Use a past deadline so next_deadline must advance past today.
        past = (date.today() - timedelta(days=5)).isoformat()
        iid = self._add("--section", "active",
                        "--title", "Monthly mtg", "--deadline", past, "--recur", "monthly")
        r = self._run("done", iid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Recurring", r.stdout)

        r2 = self._run("list")
        self.assertIn("Monthly mtg", r2.stdout)
        # Old occurrence should not appear
        r3 = self._run("list", "--json")
        items = json.loads(r3.stdout)
        self.assertEqual(len(items), 1)
        new_deadline = items[0]["deadline"]
        self.assertGreater(new_deadline, date.today().isoformat())

    def test_archive_does_not_respawn(self):
        self._init()
        soon = (date.today() + timedelta(days=5)).isoformat()
        iid = self._add("--section", "active",
                        "--title", "Recurring task", "--deadline", soon, "--recur", "weekly")
        r = self._run("archive", iid)
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._run("list", "--json")
        items = json.loads(r2.stdout)
        self.assertEqual(items, [], "archive of a recurring item must not respawn")

    def test_next_deadline_month_end_clamp(self):
        """Jan 31 + monthly → Feb 28/29 (never Feb 31)."""
        from todo import next_deadline
        result = next_deadline("2026-01-31", "monthly", "2026-01-31")
        self.assertEqual(result, "2026-02-28")

    def test_next_deadline_early_completion(self):
        """Completing before the deadline must still advance to the *next* occurrence."""
        from todo import next_deadline
        future = (date.today() + timedelta(days=10)).isoformat()
        result = next_deadline(future, "monthly", date.today().isoformat())
        # Must be strictly past the original deadline, not equal to it
        self.assertGreater(result, future)

    def test_next_deadline_advances_past_today(self):
        """Late completion: must advance until strictly after today, not just one step."""
        from todo import next_deadline
        # Deadline 3 months in the past with monthly recurrence — must jump past today.
        past = (date.today() - timedelta(days=95)).isoformat()
        result = next_deadline(past, "monthly", date.today().isoformat())
        self.assertGreater(result, date.today().isoformat())

    def test_done_early_respawns_next_occurrence(self):
        """Marking a recurring item done before its deadline respawns with the NEXT deadline."""
        self._init()
        future = (date.today() + timedelta(days=10)).isoformat()
        iid = self._add("--section", "active",
                        "--title", "Future meeting", "--deadline", future, "--recur", "monthly")
        self._run("done", iid)
        r = self._run("list", "--json")
        items = json.loads(r.stdout)
        self.assertEqual(len(items), 1)
        new_deadline = items[0]["deadline"]
        # New deadline must be strictly after the original future deadline
        self.assertGreater(new_deadline, future)

    def test_parse_recur_keywords(self):
        from todo import parse_recur
        self.assertEqual(parse_recur("monthly"), ('m', 1))
        self.assertEqual(parse_recur("weekly"),  ('w', 1))
        self.assertEqual(parse_recur("daily"),   ('d', 1))
        self.assertEqual(parse_recur("yearly"),  ('y', 1))

    def test_parse_recur_n_form(self):
        from todo import parse_recur
        self.assertEqual(parse_recur("2w"), ('w', 2))
        self.assertEqual(parse_recur("3m"), ('m', 3))
        self.assertEqual(parse_recur("14d"), ('d', 14))

    def test_parse_recur_invalid(self):
        from todo import parse_recur
        with self.assertRaises(ValueError):
            parse_recur("biweekly")
        with self.assertRaises(ValueError):
            parse_recur("")


class TestDependencies(_ProjectFixture):
    """Tests for --depends flag and blocked_by display."""

    def test_depends_add_succeeds(self):
        self._init()
        a = self._add("--section", "active", "--title", "Task A")
        r = self._run("add", "--section", "active", "--title", "Task B",
                      "--depends", a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"Added item #[0-9a-f]+")

    def test_blocked_shown_in_list(self):
        self._init()
        a = self._add("--section", "active", "--title", "Task A")
        self._run("add", "--section", "active", "--title", "Task B", "--depends", a)
        r = self._run("list")
        self.assertIn("blocked by", r.stdout)
        self.assertIn(f"#{a}", r.stdout)

    def test_unblocked_after_prerequisite_done(self):
        self._init()
        a = self._add("--section", "active", "--title", "Prereq")
        self._run("add", "--section", "active", "--title", "Downstream", "--depends", a)
        self._run("done", a)
        r = self._run("list")
        self.assertNotIn("blocked by", r.stdout)

    def test_blocked_in_json_output(self):
        self._init()
        a = self._add("--section", "active", "--title", "A")
        self._run("add", "--section", "active", "--title", "B", "--depends", a)
        r = self._run("list", "--json")
        items = json.loads(r.stdout)
        b = next(i for i in items if i["title"] == "B")
        self.assertIn("blocked_by", b)
        self.assertIn(a, b["blocked_by"])

    def test_rollup_html_shows_blocked_badge(self):
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
        subprocess.run([sys.executable, str(hub_scripts / "todo.py"), "init"],
                       cwd=str(hub), capture_output=True)
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        prereq = self._add("--section", "active", "--title", "Prereq task", "--deadline", soon)
        self._run("add", "--section", "active", "--title", "[XP] Blocked task",
                  "--deadline", soon, "--depends", prereq)

        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("blocked-badge", html)
        self.assertIn("Blocked task", html)

    def test_rollup_html_shows_recur_badge(self):
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
        subprocess.run([sys.executable, str(hub_scripts / "todo.py"), "init"],
                       cwd=str(hub), capture_output=True)
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        self._run("add", "--section", "active",
                  "--title", "[XP] Recurring meeting",
                  "--deadline", soon, "--recur", "monthly")

        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("recur-badge", html)
        self.assertIn("Recurring meeting", html)


class TestPriority(_ProjectFixture):
    """Tests for --priority flag."""

    def test_priority_add_succeeds(self):
        self._init()
        r = self._run("add", "--section", "active", "--title", "Urgent task", "--priority", "H")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"Added item #[0-9a-f]+")

    def test_invalid_priority_fails_cleanly(self):
        self._init()
        r = self._run("add", "--section", "active", "--title", "Bad priority", "--priority", "X")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("H, M, or L", r.stderr)

    def test_priority_in_json_output(self):
        self._init()
        self._run("add", "--section", "active", "--title", "HP task", "--priority", "H")
        self._run("add", "--section", "active", "--title", "LP task", "--priority", "L")
        r = self._run("list", "--json")
        items = json.loads(r.stdout)
        hp = next(i for i in items if i["title"] == "HP task")
        lp = next(i for i in items if i["title"] == "LP task")
        self.assertEqual(hp["priority"], "H")
        self.assertEqual(lp["priority"], "L")

    def test_priority_sort_order(self):
        """H must appear before L in list output when both have no deadline."""
        self._init()
        self._run("add", "--section", "active", "--title", "Low prio task",  "--priority", "L")
        self._run("add", "--section", "active", "--title", "High prio task", "--priority", "H")
        r = self._run("list")
        self.assertIn("High prio task", r.stdout)
        self.assertIn("Low prio task",  r.stdout)
        self.assertLess(r.stdout.index("High prio task"), r.stdout.index("Low prio task"))

    def test_priority_badge_in_list(self):
        self._init()
        self._run("add", "--section", "active", "--title", "Marked H", "--priority", "H")
        r = self._run("list")
        self.assertIn("[H]", r.stdout)

    def test_priority_cleared_by_update(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Task", "--priority", "H")
        self._run("update", iid, "--priority", "")
        r = self._run("list")
        self.assertNotIn("[H]", r.stdout)

    def test_rollup_html_shows_priority_badge(self):
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
        subprocess.run([sys.executable, str(hub_scripts / "todo.py"), "init"],
                       cwd=str(hub), capture_output=True)
        self._init()
        # High-priority item with no near deadline — should surface
        far_future = (date.today() + timedelta(days=365)).isoformat()
        self._run("add", "--section", "active",
                  "--title", "[XP] High pri no deadline", "--priority", "H")
        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("pri-badge", html)
        self.assertIn("High pri no deadline", html)


class TestSnooze(_ProjectFixture):
    """Tests for --snooze / --wait flag."""

    def test_snooze_hides_from_default_list(self):
        self._init()
        future = (date.today() + timedelta(days=10)).isoformat()
        self._run("add", "--section", "active", "--title", "Hidden task", "--snooze", future)
        r = self._run("list")
        self.assertNotIn("Hidden task", r.stdout)

    def test_snoozed_flag_shows_hidden(self):
        self._init()
        future = (date.today() + timedelta(days=10)).isoformat()
        self._run("add", "--section", "active", "--title", "Snoozed task", "--snooze", future)
        r = self._run("list", "--snoozed")
        self.assertIn("Snoozed task", r.stdout)

    def test_past_snooze_date_is_visible(self):
        self._init()
        past = (date.today() - timedelta(days=3)).isoformat()
        self._run("add", "--section", "active", "--title", "Past snooze", "--snooze", past)
        r = self._run("list")
        self.assertIn("Past snooze", r.stdout)

    def test_invalid_snooze_fails_cleanly(self):
        self._init()
        r = self._run("add", "--section", "active", "--title", "Bad snooze", "--snooze", "not-a-date")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)

    def test_clearing_snooze_restores_visibility(self):
        self._init()
        future = (date.today() + timedelta(days=10)).isoformat()
        iid = self._add("--section", "active", "--title", "Will show again", "--snooze", future)
        r1 = self._run("list")
        self.assertNotIn("Will show again", r1.stdout)
        self._run("update", iid, "--snooze", "")
        r2 = self._run("list")
        self.assertIn("Will show again", r2.stdout)

    def test_all_flag_shows_snoozed(self):
        self._init()
        future = (date.today() + timedelta(days=10)).isoformat()
        self._run("add", "--section", "active", "--title", "All-visible", "--snooze", future)
        r = self._run("list", "--all")
        self.assertIn("All-visible", r.stdout)

    def test_rollup_snoozed_does_not_surface(self):
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
        subprocess.run([sys.executable, str(hub_scripts / "todo.py"), "init"],
                       cwd=str(hub), capture_output=True)
        self._init()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        future_snooze = (date.today() + timedelta(days=10)).isoformat()
        # Due soon but snoozed — must NOT surface
        self._run("add", "--section", "active",
                  "--title", "[XP] Snoozed item",
                  "--deadline", tomorrow, "--snooze", future_snooze)

        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        # Item may appear in the 'all' set but must not be marked _surfaced
        self.assertIn("Snoozed item", html)  # present in data
        self.assertIn("snooze-badge", html)   # badge rendered


class TestSearch(_ProjectFixture):
    """Tests for list --search flag."""

    def test_search_matches_title(self):
        self._init()
        self._run("add", "--section", "active", "--title", "findme please")
        self._run("add", "--section", "active", "--title", "unrelated item")
        r = self._run("list", "--search", "findme")
        self.assertIn("findme please", r.stdout)
        self.assertNotIn("unrelated item", r.stdout)

    def test_search_no_match(self):
        self._init()
        self._run("add", "--section", "active", "--title", "something")
        r = self._run("list", "--search", "zzznomatch")
        self.assertIn("(no items)", r.stdout)

    def test_search_case_insensitive(self):
        self._init()
        self._run("add", "--section", "active", "--title", "CaseSensitive Test")
        r = self._run("list", "--search", "casesensitive")
        self.assertIn("CaseSensitive Test", r.stdout)

    def test_search_json(self):
        self._init()
        self._run("add", "--section", "active", "--title", "alpha task")
        self._run("add", "--section", "active", "--title", "beta task")
        r = self._run("list", "--json", "--search", "alpha")
        items = json.loads(r.stdout)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "alpha task")


class _HubFixture(_ProjectFixture):
    """Project fixture plus a hub with rollup/serve registered against it."""

    def _make_hub(self, extra_cfg=""):
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
            + extra_cfg
        )
        subprocess.run([sys.executable, str(hub_scripts / "todo.py"), "init"],
                       cwd=str(hub), capture_output=True)
        return hub, hub_scripts

    @staticmethod
    def _embedded_items(html):
        """Parse the items-data JSON block out of a generated dashboard.html."""
        blob = html.split('<script type="application/json" id="items-data">')[1]
        blob = blob.split("</script>")[0]
        return json.loads(blob)


class TestStatusTagBoundary(_ProjectFixture):
    """Keyword extraction must not match inside words (PRESENTATION ⊃ SENT)."""

    def test_keyword_not_matched_inside_word(self):
        from todo import extract_status_tag
        self.assertEqual(
            extract_status_tag("Abandoned plan awaiting revival", ["DONE"]), "OPEN")
        self.assertEqual(
            extract_status_tag("Presentation draft ready", ["SENT"]), "OPEN")

    def test_keyword_still_matched_at_boundary(self):
        from todo import extract_status_tag
        self.assertEqual(extract_status_tag("SENT to panel", ["SENT"]), "SENT")
        self.assertEqual(extract_status_tag("was sent yesterday", ["SENT"]), "SENT")
        self.assertEqual(
            extract_status_tag("**IN PROGRESS** since May", ["IN PROGRESS"]),
            "IN PROGRESS")


class TestDashboardEmbedding(_HubFixture):
    def test_html_comment_in_status_keeps_json_parseable(self):
        """A literal <!-- in status text must not break the embedded JSON."""
        hub, hub_scripts = self._make_hub()
        self._init()
        self._run("add", "--section", "active", "--title", "[XP] Banner quoter",
                  "--status", "quoting <!-- AUTO-GENERATED --> and </script> too")
        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        items = self._embedded_items(html)   # raises if the JSON is invalid
        detail = next(i for i in items if i["title"] == "[XP] Banner quoter")
        self.assertIn("<!-- AUTO-GENERATED -->", detail["status_detail"])

    def test_fingerprint_matches_server_recomputation_for_xp_item(self):
        """Embedded _fp must equal serve.py's live-row fingerprint (xp_tags set)."""
        hub, hub_scripts = self._make_hub()
        self._init()
        iid = self._add("--section", "active", "--title", "XP tagged",
                        "--xp", "OtherProj")
        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        items = self._embedded_items((hub / "dashboard.html").read_text())
        page_fp = next(i for i in items if i["title"] == "XP tagged")["_fp"]

        from rollup import item_fingerprint
        conn = sqlite3.connect(self.proj / "action_items.db")
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            "SELECT title, owner, deadline, section, status_tag, status_detail,"
            " xp_tags, recur, depends_on, priority, wait_until"
            " FROM items WHERE raw_id=?", (iid,)).fetchone())
        conn.close()
        self.assertEqual(page_fp, item_fingerprint(row))

    def test_dashboard_has_mdlite_and_theme_fix(self):
        """Generated JS carries the markdown-lite renderer and the
        localStorage theme read (behaviour itself is browser-side)."""
        hub, hub_scripts = self._make_hub()
        self._init()
        subprocess.run([sys.executable, str(hub_scripts / "rollup.py"), "--html"],
                       cwd=str(hub), capture_output=True, text=True)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("function mdLite", html)
        self.assertIn("mdLite(item.status_detail)", html)
        self.assertIn("ls.theme", html)


class TestMasterSnooze(_HubFixture):
    def test_snoozed_master_item_suppressed(self):
        hub, hub_scripts = self._make_hub()
        self._init()
        future = (date.today() + timedelta(days=10)).isoformat()
        subprocess.run(
            [sys.executable, str(hub_scripts / "todo.py"), "add",
             "--section", "active", "--title", "Snoozed master item",
             "--snooze", future],
            cwd=str(hub), capture_output=True, text=True)
        # Markdown rollup: item must not appear
        r = subprocess.run([sys.executable, str(hub_scripts / "rollup.py")],
                           cwd=str(hub), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Snoozed master item",
                         (hub / "MASTER_PRIORITIES.md").read_text())
        # HTML rollup: present in the data but not surfaced
        subprocess.run([sys.executable, str(hub_scripts / "rollup.py"), "--html"],
                       cwd=str(hub), capture_output=True, text=True)
        items = self._embedded_items((hub / "dashboard.html").read_text())
        item = next(i for i in items if i["title"] == "Snoozed master item")
        self.assertFalse(item["_surfaced"])


class TestApiClearFields(_HubFixture):
    def test_api_update_empty_string_clears_deadline_and_recur(self):
        """'' from the edit modal must clear the field (was a silent no-op)."""
        hub, hub_scripts = self._make_hub()
        self._init()
        future = (date.today() + timedelta(days=30)).isoformat()
        iid = self._add("--section", "active", "--title", "Clear me",
                        "--deadline", future, "--recur", "monthly", "--owner", "GN")

        from rollup import item_fingerprint
        conn = sqlite3.connect(self.proj / "action_items.db")
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            "SELECT title, owner, deadline, section, status_tag, status_detail,"
            " xp_tags, recur, depends_on, priority, wait_until"
            " FROM items WHERE raw_id=?", (iid,)).fetchone())
        conn.close()
        base_fp = item_fingerprint(row)

        port = TestServe._free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [sys.executable, str(hub_scripts / "serve.py"),
             "--port", str(port), "--no-browser"],
            cwd=str(hub), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(50):
                try:
                    with urllib.request.urlopen(base + "/api/ping", timeout=1) as resp:
                        if resp.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("server did not become ready")

            payload = {"project": "proj", "id": iid, "base_fp": base_fp,
                       "section": "active", "title": "Clear me",
                       "owner": "GN", "deadline": "", "recur": "",
                       "depends_on": "", "priority": "", "wait_until": "",
                       "xp_tags": "", "status_tag": None}
            req = urllib.request.Request(base + "/api/update",
                                         data=json.dumps(payload).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Tracker", "1")
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.assertTrue(json.load(resp).get("ok"))

            conn = sqlite3.connect(self.proj / "action_items.db")
            deadline, recur = conn.execute(
                "SELECT deadline, recur FROM items WHERE raw_id=?", (iid,)).fetchone()
            conn.close()
            self.assertIsNone(deadline)   # NULL, not ''
            self.assertIsNone(recur)
        finally:
            server.terminate()
            server.wait(timeout=5)
            server.stdout.close()


class TestDeadlineValidation(_ProjectFixture):
    def test_update_deadline_empty_clears_to_null(self):
        self._init()
        future = (date.today() + timedelta(days=5)).isoformat()
        iid = self._add("--section", "active", "--title", "Dated", "--deadline", future)
        r = self._run("update", iid, "--deadline", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.proj / "action_items.db")
        deadline = conn.execute("SELECT deadline FROM items WHERE raw_id=?", (iid,)).fetchone()[0]
        conn.close()
        self.assertIsNone(deadline)

    def test_add_invalid_deadline_fails_cleanly(self):
        self._init()
        r = self._run("add", "--section", "active", "--title", "Bad date",
                      "--deadline", "next tuesday")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("expected YYYY-MM-DD", r.stderr)

    def test_update_invalid_deadline_fails_cleanly(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Item")
        r = self._run("update", iid, "--deadline", "2026-13-45")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("expected YYYY-MM-DD", r.stderr)


class TestIdReuse(_ProjectFixture):
    def test_id_not_reused_after_closing_item(self):
        """A closed item's hash id stays in issued_ids so it can never be reissued."""
        self._init()
        self._add("--section", "active", "--title", "First")
        second = self._add("--section", "active", "--title", "Second")
        self._run("done", second)
        third = self._add("--section", "active", "--title", "Third")
        self.assertNotEqual(third, second, "closed item's id must not be recycled")
        conn = sqlite3.connect(self.proj / "action_items.db")
        issued = {r[0] for r in conn.execute("SELECT id FROM issued_ids").fetchall()}
        conn.close()
        self.assertIn(second, issued, "closed id must stay registered as issued")

    def test_respawn_gets_fresh_id(self):
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        iid = self._add("--section", "active", "--title", "Weekly",
                        "--deadline", soon, "--recur", "weekly")
        r = self._run("done", iid)
        self.assertEqual(r.returncode, 0, r.stderr)
        items = json.loads(self._run("list", "--json").stdout)
        self.assertEqual(len(items), 1)
        self.assertNotEqual(items[0]["raw_id"], iid)

    def test_pre_meta_db_seeds_counter_from_max_sort_id(self):
        """A DB created before the meta table gets a correct seeded sort counter."""
        self._init()
        self._add("--section", "active", "--title", "Legacy")
        conn = sqlite3.connect(self.proj / "action_items.db")
        conn.execute("DELETE FROM meta")   # simulate a pre-counter DB
        conn.commit()
        conn.close()
        r = self._run("add", "--section", "active", "--title", "Post-migration")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.proj / "action_items.db")
        sort_ids = sorted(r[0] for r in conn.execute("SELECT sort_id FROM items").fetchall())
        conn.close()
        self.assertEqual(sort_ids, [1, 2])


class TestRespawnPriority(_ProjectFixture):
    def test_respawn_preserves_priority(self):
        self._init()
        soon = (date.today() + timedelta(days=3)).isoformat()
        iid = self._add("--section", "active", "--title", "Weekly report",
                        "--deadline", soon, "--recur", "weekly", "--priority", "H")
        self._run("done", iid)
        items = json.loads(self._run("list", "--json").stdout)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["priority"], "H")


class TestClosedTagWarning(_ProjectFixture):
    def test_update_to_closed_tag_warns(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Zombie candidate")
        r = self._run("update", iid, "--tag", "DONE")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING", r.stderr)
        self.assertIn("done", r.stderr)


class TestSearchEscaping(_ProjectFixture):
    def test_percent_is_literal_in_search(self):
        self._init()
        self._run("add", "--section", "active", "--title", "has % sign")
        self._run("add", "--section", "active", "--title", "plain item")
        r = self._run("list", "--search", "%")
        self.assertIn("has % sign", r.stdout)
        self.assertNotIn("plain item", r.stdout)

    def test_underscore_is_literal_in_search(self):
        self._init()
        self._run("add", "--section", "active", "--title", "snake_case name")
        self._run("add", "--section", "active", "--title", "abc")
        r = self._run("list", "--search", "e_c")
        self.assertIn("snake_case name", r.stdout)
        self.assertNotIn("abc", r.stdout)


class TestClosedTagQuoting(_ProjectFixture):
    def test_closed_tag_with_apostrophe_does_not_break_queries(self):
        (self.proj / "scripts" / "tracker_config.py").write_text(
            'PROJECT_TITLE = "Smoke Test"\n'
            'SECTION_ORDER = [("active", "Active"), ("backlog", "Backlog")]\n'
            'STANDING_SLUG = "backlog"\n'
            "CLOSED_TAGS = frozenset([\"DONE\", \"WON'T DO\"])\n"
        )
        self._init()
        self._run("add", "--section", "active", "--title", "Alive item")
        r = self._run("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Alive item", r.stdout)


class TestDonePrefix(_ProjectFixture):
    def test_bold_done_prefix_not_duplicated_in_archive(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Pre-marked",
                        "--status", "**DONE 2026-01-01** — wrapped up earlier")
        r = self._run("done", iid)
        self.assertEqual(r.returncode, 0, r.stderr)
        archive = (self.proj / "action_items_archive.md").read_text()
        row = next(l for l in archive.splitlines() if "Pre-marked" in l)
        self.assertEqual(row.count("DONE"), 1, f"double prefix in: {row}")


class TestReady(_ProjectFixture):
    def _seed(self):
        self._init()
        self.id_a = self._add("--section", "active", "--title", "Unblocked A",
                              "--deadline", (date.today() + timedelta(days=5)).isoformat())
        self.id_b = self._add("--section", "active", "--title", "Blocked B",
                              "--depends", self.id_a)
        self.id_c = self._add("--section", "active", "--title", "Snoozed C",
                              "--snooze", (date.today() + timedelta(days=30)).isoformat())

    def test_ready_excludes_blocked_and_snoozed(self):
        self._seed()
        r = self._run("ready", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [it["raw_id"] for it in json.loads(r.stdout)]
        self.assertEqual(ids, [self.id_a])

    def test_ready_includes_item_after_blocker_done(self):
        self._seed()
        self._run("done", self.id_a)
        r = self._run("ready", "--json")
        ids = [it["raw_id"] for it in json.loads(r.stdout)]
        self.assertIn(self.id_b, ids)

    def test_ready_excludes_standing(self):
        self._init()
        self._run("add", "--section", "backlog", "--title", "Standing item")
        r = self._run("ready")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Standing item", r.stdout)


class TestClaim(_ProjectFixture):
    def test_claim_sets_in_progress_and_owner(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Claimable")
        r = self._run("claim", iid, "--by", "agent-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        items = {it["raw_id"]: it for it in json.loads(self._run("list", "--json").stdout)}
        self.assertEqual(items[iid]["status_tag"], "IN PROGRESS")
        self.assertEqual(items[iid]["owner"], "agent-1")

    def test_second_claim_fails(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Contended")
        self._run("claim", iid, "--by", "agent-1")
        r = self._run("claim", iid, "--by", "agent-2")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already claimed", r.stderr)

    def test_claim_preserves_existing_owner(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Owned", "--owner", "Alice")
        self._run("claim", iid, "--by", "agent-1")
        items = {it["raw_id"]: it for it in json.loads(self._run("list", "--json").stdout)}
        self.assertEqual(items[iid]["owner"], "Alice")

    def test_release_then_reclaim(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Recycled")
        self._run("claim", iid, "--by", "agent-1")
        self._run("update", iid, "--tag", "OPEN")
        r = self._run("claim", iid, "--by", "agent-2")
        self.assertEqual(r.returncode, 0, r.stderr)


class TestPrime(_ProjectFixture):
    def test_prime_counts_and_sections(self):
        self._init()
        first = self._add("--section", "active", "--title", "Overdue item",
                          "--deadline", (date.today() - timedelta(days=2)).isoformat())
        self._run("add", "--section", "active", "--title", "Blocked item", "--depends", first)
        r = self._run("prime")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 open", r.stdout)
        self.assertIn("1 overdue", r.stdout)
        self.assertIn("1 ready", r.stdout)
        self.assertIn("1 blocked", r.stdout)
        self.assertIn("## Overdue", r.stdout)
        self.assertIn("## Conventions", r.stdout)

    def test_prime_on_empty_db(self):
        self._init()
        r = self._run("prime")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 open", r.stdout)


class TestDashboardReadyView(_HubFixture):
    def test_html_has_ready_view(self):
        hub, hub_scripts = self._make_hub()
        self._init()
        self._run("add", "--section", "active", "--title", "Ready view seed")
        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("{id:'ready',label:'Ready'}", html)
        self.assertIn("case 'ready'", html)


class TestMigrateIds(_ProjectFixture):
    """migrate-ids: numeric raw_ids -> hash ids with legacy_id fallback."""

    def _seed_legacy(self):
        """Create items, then rewrite their ids to the pre-hash numeric scheme."""
        self._init()
        a = self._add("--section", "active", "--title", "Legacy A")
        b = self._add("--section", "active", "--title", "Legacy B", "--depends", a)
        conn = sqlite3.connect(self.proj / "action_items.db")
        conn.execute("UPDATE items SET raw_id='1' WHERE raw_id=?", (a,))
        conn.execute("UPDATE items SET raw_id='2', depends_on='1' WHERE raw_id=?", (b,))
        conn.commit()
        conn.close()

    def test_migrate_rewrites_ids_and_depends(self):
        self._seed_legacy()
        r = self._run("migrate-ids")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Migrated 2 items", r.stdout)
        items = json.loads(self._run("list", "--json").stdout)
        by_title = {i["title"]: i for i in items}
        new_a = by_title["Legacy A"]
        new_b = by_title["Legacy B"]
        for it in (new_a, new_b):
            self.assertRegex(it["raw_id"], r"^[0-9a-f]+$")
            self.assertTrue(any(c.isalpha() for c in it["raw_id"]))
        self.assertEqual(new_a["legacy_id"], "1")
        self.assertEqual(new_b["legacy_id"], "2")
        self.assertEqual(new_b["depends_on"], new_a["raw_id"])
        self.assertEqual(new_b["blocked_by"], [new_a["raw_id"]])

    def test_migrate_writes_backup(self):
        self._seed_legacy()
        self._run("migrate-ids")
        backups = list(self.proj.glob("action_items.db.bak-*"))
        self.assertEqual(len(backups), 1)

    def test_legacy_id_lookup_still_works(self):
        self._seed_legacy()
        self._run("migrate-ids")
        r = self._run("show", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Legacy A", r.stdout)
        r = self._run("update", "2", "--priority", "H")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("done", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("list", "--json")
        titles = [i["title"] for i in json.loads(r.stdout)]
        self.assertNotIn("Legacy A", titles)

    def test_migrate_noop_on_hash_only_db(self):
        self._init()
        self._add("--section", "active", "--title", "Already hashed")
        r = self._run("migrate-ids")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nothing to migrate", r.stdout)
        self.assertEqual(list(self.proj.glob("action_items.db.bak-*")), [])

    def test_depends_on_legacy_id_normalized_at_write(self):
        self._seed_legacy()
        self._run("migrate-ids")
        items = {i["title"]: i for i in json.loads(self._run("list", "--json").stdout)}
        new_c = self._add("--section", "active", "--title", "New C", "--depends", "1")
        c = json.loads(self._run("list", "--json").stdout)
        c_item = next(i for i in c if i["title"] == "New C")
        self.assertEqual(c_item["depends_on"], items["Legacy A"]["raw_id"])


class TestSortIdAlias(_ProjectFixture):
    """sort_id continues the pre-hash numbering as a stable numeric alias."""

    def _sort_id_of(self, iid):
        items = {i["raw_id"]: i for i in json.loads(self._run("list", "--json").stdout)}
        return items[iid]["sort_id"]

    def test_new_item_resolvable_by_sort_id(self):
        self._init()
        iid = self._add("--section", "active", "--title", "Aliased item")
        sid = self._sort_id_of(iid)
        r = self._run("show", str(sid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Aliased item", r.stdout)
        r = self._run("done", str(sid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Aliased item", self._run("list").stdout)

    def test_sort_ids_continue_monotone(self):
        self._init()
        a = self._add("--section", "active", "--title", "First")
        b = self._add("--section", "active", "--title", "Second")
        self.assertEqual(self._sort_id_of(b), self._sort_id_of(a) + 1)

    def test_hash_lookup_wins_over_numeric(self):
        """A raw_id/legacy match must take precedence over the sort_id alias."""
        self._init()
        iid = self._add("--section", "active", "--title", "Precedence")
        sid = self._sort_id_of(iid)
        r = self._run("show", iid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"sort_id       : {sid}", r.stdout)


class TestDashboardNumRef(_HubFixture):
    def test_html_embeds_sort_id_and_num_ref_search(self):
        hub, hub_scripts = self._make_hub()
        self._init()
        self._run("add", "--section", "active", "--title", "Num ref seed")
        r = subprocess.run(
            [sys.executable, str(hub_scripts / "rollup.py"), "--html"],
            cwd=str(hub), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (hub / "dashboard.html").read_text()
        self.assertIn("function numRefLabel", html)
        items = self._embedded_items(html)
        seed = next(i for i in items if i["title"] == "Num ref seed")
        self.assertIsInstance(seed["sort_id"], int)


if __name__ == "__main__":
    unittest.main()
