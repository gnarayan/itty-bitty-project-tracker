#!/usr/bin/env python3
"""Smoke tests for itty-bitty-project-tracker (stdlib only, no deps).

Usage:
  python3 -m unittest tests/test_smoke.py -v
  python3 tests/test_smoke.py
"""
import concurrent.futures
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
