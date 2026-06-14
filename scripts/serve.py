#!/usr/bin/env python3
"""
Local companion server for the cross-project priorities dashboard.

Serves dashboard.html at http://127.0.0.1:PORT and exposes write endpoints:
  POST /api/add     — create a new task in a project DB
  POST /api/update  — edit an existing task's structured fields
  POST /api/done    — mark a task done or archive it

All write paths validate inputs against a project/section whitelist, check
an optimistic-concurrency fingerprint (reject stale writes), and hold a
threading lock to prevent concurrent write conflicts.

Usage:
  python3 scripts/serve.py [--port 8765] [--no-browser]

Security:
  - Bound to 127.0.0.1 only — not reachable from other hosts.
  - CSRF / DNS-rebinding guard on all write endpoints: Host must be
    127.0.0.1:PORT or localhost:PORT; Origin (when present) must match; custom
    X-Tracker header required (cross-origin simple requests cannot set it
    without a CORS preflight, which the server does not grant).
  - project and section inputs are validated against build_projects_meta()
    before any subprocess is spawned.
  - Subprocess calls use argument lists (shell=False) — no shell injection.
  - Writes serialized in-process via _WRITE_LOCK; SQLite busy_timeout handles
    cross-process contention (e.g. concurrent CLI edits or rollup reads).
"""
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import rollup  # noqa: E402  (same scripts/ dir)

BASE      = rollup.BASE
HTML_PATH = rollup.HTML_OUT_PATH
ROLLUP_PY = _HERE / "rollup.py"

DEFAULT_PORT = 8765

# Serialize all write operations: add, update, done/archive.
# Prevents concurrent browser submissions from interleaving writes + regen.
_WRITE_LOCK = threading.Lock()

# Module logger. Configured by _setup_logging() in main(); until then it is a
# no-op so importing this module (e.g. for tests) never emits stray output.
log = logging.getLogger("tracker")


def _setup_logging(log_file):
    """Route server output to a rotating file, or to stdout when no file given.

    With a log file: RotatingFileHandler caps total on-disk size at
    maxBytes*(backupCount+1) (~6 MB here) so a long-running background server
    never grows the log without bound. Without one: stdout, preserving the
    interactive foreground behaviour.
    """
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    log.addHandler(handler)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_project(project):
    """Validate project label against live metadata.

    Returns (proj_dir: Path, todo_py: Path, error: str|None).
    error is None on success.
    """
    meta = rollup.build_projects_meta()
    proj_entry = next((p for p in meta if p["label"] == project), None)
    if proj_entry is None:
        known = [p["label"] for p in meta]
        return None, None, f"unknown project {project!r}; known: {known}"
    proj_dir = Path(proj_entry["dir"])
    todo_py  = proj_dir / "scripts" / "todo.py"
    if not todo_py.exists():
        return None, None, f"todo.py not found at {todo_py}"
    return proj_dir, todo_py, None


def _regen(proj_dir, todo_py):
    """Best-effort: export the project MD then regenerate master MD + HTML.

    Returns a warning string if the dashboard regen failed, else None.
    The DB write already succeeded; the warning is informational only.
    """
    subprocess.run([sys.executable, str(todo_py), "export"],
                   cwd=str(proj_dir), capture_output=True, text=True)
    subprocess.run([sys.executable, str(ROLLUP_PY)],
                   cwd=str(BASE), capture_output=True, text=True)
    r = subprocess.run([sys.executable, str(ROLLUP_PY), "--html"],
                       cwd=str(BASE), capture_output=True, text=True)
    if r.returncode != 0:
        msg = r.stderr.strip() or r.stdout.strip() or "unknown error"
        log.warning("[regen] rollup --html failed: %s", msg)
        return "dashboard regen failed; reload to retry"
    return None


def _check_fingerprint(proj_dir, raw_id, base_fp):
    """Verify the client's base_fp against the live DB row.

    Returns (current_row: dict|None, conflict: bool, error: str|None).
    conflict=True means the row has been modified since the page loaded.
    """
    db_path = proj_dir / "action_items.db"
    if not db_path.exists():
        return None, False, f"DB not found: {db_path}"
    conn = rollup.open_ro(db_path)
    cur  = conn.cursor()
    cur.execute(
        "SELECT title, owner, deadline, section, status_tag, status_detail, xp_tags "
        "FROM items WHERE raw_id = ?",
        (raw_id,)
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None, True, f"item {raw_id!r} not found (already closed?)"
    d = dict(row)
    live_fp = rollup.item_fingerprint(d)
    if live_fp != base_fp:
        return d, True, "task changed since the page loaded — please refresh"
    return d, False, None


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------

def _validate_and_add(payload):
    """Validate payload, run todo.py add, then regenerate the dashboard.

    Returns (http_status: int, result: dict).
    """
    project  = (payload.get("project")  or "").strip()
    section  = (payload.get("section")  or "").strip()
    title    = (payload.get("title")    or "").strip()
    owner    = (payload.get("owner")    or "").strip() or None
    deadline = (payload.get("deadline") or "").strip() or None
    xp_tags  = (payload.get("xp_tags")  or "").strip() or None

    if not title:
        return 400, {"ok": False, "error": "title is required"}

    proj_dir, todo_py, err = _resolve_project(project)
    if err:
        return 400, {"ok": False, "error": err}

    # Validate section within project
    meta = rollup.build_projects_meta()
    proj_entry = next(p for p in meta if p["label"] == project)
    valid_sections = {s[0] for s in proj_entry["sections"]}
    if section not in valid_sections:
        return 400, {"ok": False, "error": f"unknown section {section!r} for {project!r}; valid: {sorted(valid_sections)}"}

    cmd = [sys.executable, str(todo_py), "add",
           "--section", section, "--title", title]
    if owner:    cmd += ["--owner",    owner]
    if deadline: cmd += ["--deadline", deadline]
    if xp_tags:  cmd += ["--xp",      xp_tags]

    with _WRITE_LOCK:
        r = subprocess.run(cmd, cwd=str(proj_dir), capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or "todo.py add failed")
            return 400, {"ok": False, "error": err}
        m = re.search(r"#(\w+)", r.stdout)
        new_id = m.group(1) if m else None
        warning = _regen(proj_dir, todo_py)

    result = {"ok": True, "id": new_id, "stdout": r.stdout.strip()}
    if warning:
        result["warning"] = warning
    return 200, result


def _validate_and_update(payload):
    """Validate + optimistic-check, then run todo.py update.

    Returns (http_status: int, result: dict).
    """
    project    = (payload.get("project")    or "").strip()
    raw_id     = (payload.get("id")         or "").strip()
    base_fp    = (payload.get("base_fp")    or "").strip()
    section    = (payload.get("section")    or "").strip() or None
    title      = (payload.get("title")      or "").strip() or None
    owner      = payload.get("owner")       # may be None to clear
    deadline   = payload.get("deadline")    # may be None to clear
    status_tag = (payload.get("status_tag") or "").strip().upper() or None
    xp_tags    = payload.get("xp_tags")     # may be None to clear

    if not raw_id:
        return 400, {"ok": False, "error": "id is required"}
    if title is not None and not title:
        return 400, {"ok": False, "error": "title cannot be empty"}

    proj_dir, todo_py, err = _resolve_project(project)
    if err:
        return 400, {"ok": False, "error": err}

    with _WRITE_LOCK:
        _, conflict, cerr = _check_fingerprint(proj_dir, raw_id, base_fp)
        if conflict:
            return 409, {"ok": False, "conflict": True, "error": cerr}

        cmd = [sys.executable, str(todo_py), "update", raw_id]
        if title      is not None: cmd += ["--title",    title]
        if owner      is not None: cmd += ["--owner",    owner]
        if deadline   is not None: cmd += ["--deadline", deadline]
        if section    is not None: cmd += ["--section",  section]
        if status_tag is not None: cmd += ["--tag",      status_tag]
        if xp_tags    is not None: cmd += ["--xp",       xp_tags]

        r = subprocess.run(cmd, cwd=str(proj_dir), capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or "todo.py update failed")
            return 400, {"ok": False, "error": err}
        warning = _regen(proj_dir, todo_py)

    result = {"ok": True, "stdout": r.stdout.strip()}
    if warning:
        result["warning"] = warning
    return 200, result


def _validate_and_close(payload):
    """Validate + optimistic-check, then run todo.py done|archive.

    Returns (http_status: int, result: dict).
    """
    project = (payload.get("project") or "").strip()
    raw_id  = (payload.get("id")      or "").strip()
    base_fp = (payload.get("base_fp") or "").strip()
    mode    = (payload.get("mode")    or "").strip().lower()

    if not raw_id:
        return 400, {"ok": False, "error": "id is required"}
    if mode not in {"done", "archive"}:
        return 400, {"ok": False, "error": f"mode must be 'done' or 'archive', got {mode!r}"}

    proj_dir, todo_py, err = _resolve_project(project)
    if err:
        return 400, {"ok": False, "error": err}

    with _WRITE_LOCK:
        _, conflict, cerr = _check_fingerprint(proj_dir, raw_id, base_fp)
        if conflict:
            return 409, {"ok": False, "conflict": True, "error": cerr}

        cmd = [sys.executable, str(todo_py), mode, raw_id]
        r = subprocess.run(cmd, cwd=str(proj_dir), capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or f"todo.py {mode} failed")
            return 400, {"ok": False, "error": err}
        # cmd_done_archive already calls _export internally; still regen master.
        warning = _regen(proj_dir, todo_py)

    result = {"ok": True, "stdout": r.stdout.strip()}
    if warning:
        result["warning"] = warning
    return 200, result


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    _PORT = DEFAULT_PORT   # overridden in main() with the actual --port value

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/ping":
            self._json(200, {"ok": True})
            return

        if path in ("/", "/dashboard.html"):
            try:
                body = HTML_PATH.read_bytes()
            except FileNotFoundError:
                self._json(404, {"error": "dashboard.html not found — run: python3 scripts/rollup.py --html"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/add", "/api/update", "/api/done"):
            self.send_response(404)
            self.end_headers()
            return

        # CSRF / DNS-rebinding guard.  Requires:
        #   1. A Host header we recognise (prevents rebinding attacks that
        #      reroute a foreign domain to 127.0.0.1).
        #   2. Origin (when present) must match the allowed host (rejects
        #      cross-origin fetch() attempts with a mismatched origin).
        #   3. X-Tracker custom header (cross-origin "simple" requests with
        #      Content-Type: application/json always trigger a preflight, which
        #      the server never grants — so a foreign page cannot forge it).
        port = Handler._PORT
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        x_tracker = self.headers.get("X-Tracker", "")
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed_hosts:
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        if origin and origin not in {f"http://{h}" for h in allowed_hosts}:
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        if not x_tracker:
            self._json(403, {"ok": False, "error": "forbidden"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid JSON body"})
            return

        if path == "/api/add":
            status, result = _validate_and_add(payload)
        elif path == "/api/update":
            status, result = _validate_and_update(payload)
        else:  # /api/done
            status, result = _validate_and_close(payload)

        self._json(status, result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser(description="Local server for the priorities dashboard")
    p.add_argument("--port",       type=int, default=DEFAULT_PORT,
                   help=f"Port to listen on (default: {DEFAULT_PORT})")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't open the browser automatically")
    p.add_argument("--log-file", default=None,
                   help="Write logs to this file (rotating, ~6 MB cap) instead "
                        "of stdout. Also honours the TRACKER_LOG_FILE env var.")
    args = p.parse_args()

    log_file = args.log_file or os.environ.get("TRACKER_LOG_FILE")
    _setup_logging(log_file)

    url = f"http://127.0.0.1:{args.port}"
    log.info("Priorities dashboard  ->  %s", url)
    log.info("Write endpoints       ->  POST %s/api/add | /api/update | /api/done", url)
    if not log_file:
        log.info("Press Ctrl-C to stop.")

    if not args.no_browser:
        webbrowser.open(url)

    Handler._PORT = args.port
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped.")


if __name__ == "__main__":
    main()
