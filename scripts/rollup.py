#!/usr/bin/env python3
"""
Cross-project priority rollup.

Reads the master priorities DB (this project's action_items.db) plus each
per-project action_items.db registered in PROJECTS (tracker_config.py).
Surfaces:
  - All open items in the master DB (proposals + jobs — unconditional).
  - Open items from each project that are either:
      * flagged [XP] in the title (explicit cross-project priority), OR
      * have a deadline within the next ROLLUP_WINDOW_DAYS days, OR
      * have a non-empty xp_tags column (cross-project tag).
Merges and sorts by deadline (NULLs last), writes MASTER_PRIORITIES.md.

Usage:
  python3 scripts/rollup.py [--window-days N] [--output FILE] [--json] [--html]
  python3 scripts/rollup.py --help

Flags:
  --window-days N   Override ROLLUP_WINDOW_DAYS from config (default: 60).
  --output FILE     Write to FILE instead of MASTER_PRIORITIES.md (or dashboard.html for --html).
  --json            Print JSON to stdout instead of writing the file.
  --html            Write a self-contained HTML dashboard (dashboard.html) instead of markdown.
                    Embeds ALL open items per project (not just window-surfaced ones) so
                    project chips can drill down beyond the rollup window.

Path resolution:
  Each PROJECTS entry is (label, path_tail).  For a relative tail, rollup.py
  probes <root>/<tail>/action_items.db across the roots returned by
  _project_roots() (common cloud-sync mounts — Dropbox/Box/OneDrive/Drive — plus
  $HOME), using whichever exists; set PROJECT_ROOTS in tracker_config.py to be
  explicit.  If none exists, a warning is printed to stderr and that project is
  skipped.  This allows graceful degradation on machines where some projects are
  not synced (e.g. tablet/Termux with selective bisync).

Sentinel:
  Prepend [XP] to any item's title in any project DB via:
    python3 scripts/todo.py update <id> --title "[XP] original title"
  to force it into the rollup regardless of deadline.

  Cross-project tags (xp_tags column):
    python3 scripts/todo.py update <id> --xp ProjectA,ProjectB
  Tags an item as cross-project; it will surface in the rollup and appear
  under the tagged projects in the dashboard filter.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# Pin streams to UTF-8 (same guard as todo.py)
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

try:
    import tracker_config as _cfg
except ImportError:
    _cfg = None


def _c(name, default):
    return getattr(_cfg, name, default) if _cfg else default


BASE          = _HERE.parent
DB_PATH       = BASE / "action_items.db"
MD_OUT_PATH   = BASE / "MASTER_PRIORITIES.md"
HTML_OUT_PATH = BASE / "dashboard.html"

PROJECT_TITLE     = _c("PROJECT_TITLE", "Cross-Project Priorities")
PROJECTS          = _c("PROJECTS", [])          # [(label, path_tail), ...]
ROLLUP_WINDOW_DAYS= _c("ROLLUP_WINDOW_DAYS", 60)
XP_SENTINEL       = "[XP]"

# Tags treated as "closed" — excluded from the rollup. Override CLOSED_TAGS in
# tracker_config.py to match your own taxonomy (keep it in sync with todo.py).
CLOSED_TAGS = frozenset(_c("CLOSED_TAGS", [
    "DONE", "ARCHIVED", "CANCELLED",
]))

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _project_roots():
    """Return candidate base directories for resolving relative project paths.

    Override via PROJECT_ROOTS in tracker_config.py, e.g.:
        PROJECT_ROOTS = ["/home/user/work", "/mnt/shared"]
    Defaults to the common cloud-sync mounts (Dropbox/Box/OneDrive/Drive) and
    $HOME, existence-filtered, so a relative tail resolves out-of-the-box on
    most setups; set PROJECT_ROOTS to be explicit.
    """
    custom = _c("PROJECT_ROOTS", None)
    if custom:
        return [Path(r).expanduser() for r in custom]
    home = Path.home()
    roots = []
    cloud = home / "Library" / "CloudStorage"      # macOS File Provider mounts
    if cloud.is_dir():
        roots += sorted(cloud.iterdir())            # Dropbox, Box-Box, OneDrive-*, GoogleDrive-*
    roots += [home / "Dropbox", home / "Box", home / "OneDrive",
              home / "Google Drive", home]
    return [r for r in roots if r.exists()]


def resolve_project_db(path_or_tail):
    """Return Path to a project's action_items.db, or None if not found.

    path_or_tail may be:
    - An absolute path (or ~/…): used directly as the project directory.
    - A relative path: resolved against each root in _project_roots() in order.
    """
    p = Path(path_or_tail).expanduser()
    if p.is_absolute():
        candidate = p / "action_items.db"
        return candidate if candidate.exists() else None
    for root in _project_roots():
        candidate = root / path_or_tail / "action_items.db"
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_ro(db_path):
    """Open a SQLite DB read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def item_fingerprint(d):
    """SHA-1 of the mutable task fields — used for optimistic-concurrency checks.

    Client embeds the fingerprint at page-render time; on write the server
    recomputes it from the live DB row and rejects the request if they differ
    (meaning the row was modified since the page loaded).

    Only fields editable via /api/update or /api/done are included.
    """
    parts = (
        d.get("title")         or "",
        d.get("owner")         or "",
        d.get("deadline")      or "",
        d.get("section")       or "",
        d.get("status_tag")    or "",
        d.get("status_detail") or "",
        d.get("xp_tags")       or "",
        d.get("recur")         or "",
        d.get("depends_on")    or "",
        d.get("priority")      or "",
        d.get("wait_until")    or "",
    )
    raw = "\x00".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _has_column(conn, table, column):
    """Return True if column exists in table (checked via PRAGMA)."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _parse_xp_tags(xp_str):
    """Parse a comma-separated xp_tags string into a list of non-empty labels."""
    if not xp_str:
        return []
    return [t.strip() for t in xp_str.split(",") if t.strip()]


def fetch_master_items(cutoff_date_str):
    """All open items from the master priorities DB (unconditional)."""
    if not DB_PATH.exists():
        sys.exit(f"Master DB not found: {DB_PATH}\nRun: python3 scripts/todo.py init")
    conn = open_ro(DB_PATH)
    cur  = conn.cursor()
    closed = ",".join("?" for _ in CLOSED_TAGS)
    has_xp     = _has_column(conn, "items", "xp_tags")
    has_recur  = _has_column(conn, "items", "recur")
    has_deps   = _has_column(conn, "items", "depends_on")
    has_pri    = _has_column(conn, "items", "priority")
    has_wait   = _has_column(conn, "items", "wait_until")
    xp_col     = ", xp_tags"    if has_xp    else ""
    recur_col  = ", recur"      if has_recur  else ""
    deps_col   = ", depends_on" if has_deps   else ""
    pri_col    = ", priority"   if has_pri    else ""
    wait_col   = ", wait_until" if has_wait   else ""
    cur.execute(f"""
        SELECT raw_id, section, title, owner, deadline, status_tag, status_detail,
               is_standing{xp_col}{recur_col}{deps_col}{pri_col}{wait_col}
        FROM items
        WHERE status_tag NOT IN ({closed}) AND is_standing = 0
        ORDER BY CASE WHEN deadline IS NULL THEN '9999' ELSE deadline END, sort_id
    """, tuple(CLOSED_TAGS))
    rows = cur.fetchall()
    # Active IDs for blocked_by derivation (any row present = not closed)
    cur.execute("SELECT raw_id FROM items")
    active_ids = {row[0] for row in cur.fetchall()}
    conn.close()
    today_iso = date.today().isoformat()
    result = []
    for r in rows:
        d = dict(r)
        d["_project"] = "Master"
        d["_db"] = str(DB_PATH)
        d["_xp_tags"] = _parse_xp_tags(d.get("xp_tags"))
        dep_ids = [x.strip() for x in (d.get("depends_on") or "").split(",") if x.strip()]
        d["blocked_by"] = [x for x in dep_ids if x in active_ids]
        # Master items are otherwise unconditional, but snoozing must still hide them.
        wait = d.get("wait_until") or ""
        d["_surfaced"] = not (wait and wait > today_iso)
        result.append(d)
    return result


def fetch_project_items(label, db_path, cutoff_date_str):
    """Open items from a project DB that are [XP]-tagged, xp_tags-set, or within the window."""
    conn = open_ro(db_path)
    cur  = conn.cursor()
    closed = ",".join("?" for _ in CLOSED_TAGS)
    has_xp    = _has_column(conn, "items", "xp_tags")
    has_recur = _has_column(conn, "items", "recur")
    has_deps  = _has_column(conn, "items", "depends_on")
    has_pri   = _has_column(conn, "items", "priority")
    has_wait  = _has_column(conn, "items", "wait_until")
    xp_col    = ", xp_tags"    if has_xp    else ""
    recur_col = ", recur"      if has_recur  else ""
    deps_col  = ", depends_on" if has_deps   else ""
    pri_col   = ", priority"   if has_pri    else ""
    wait_col  = ", wait_until" if has_wait   else ""
    xp_cond   = "OR (xp_tags IS NOT NULL AND xp_tags != '')" if has_xp else ""
    pri_cond  = "OR (priority = 'H')" if has_pri else ""
    cur.execute(f"""
        SELECT raw_id, section, title, owner, deadline, status_tag, status_detail,
               is_standing{xp_col}{recur_col}{deps_col}{pri_col}{wait_col}
        FROM items
        WHERE status_tag NOT IN ({closed})
          AND is_standing = 0
          AND (
            title LIKE '%[XP]%'
            OR (deadline IS NOT NULL AND deadline <= ?)
            {xp_cond}
            {pri_cond}
          )
        ORDER BY CASE WHEN deadline IS NULL THEN '9999' ELSE deadline END, sort_id
    """, (*CLOSED_TAGS, cutoff_date_str))
    rows = cur.fetchall()
    cur.execute("SELECT raw_id FROM items")
    active_ids = {row[0] for row in cur.fetchall()}
    conn.close()
    today_iso = date.today().isoformat()
    result = []
    for r in rows:
        d = dict(r)
        # Snoozed items are suppressed even if otherwise surfaced
        if d.get("wait_until") and d["wait_until"] > today_iso:
            continue
        d["_project"] = label
        d["_db"] = str(db_path)
        d["_xp_tags"] = _parse_xp_tags(d.get("xp_tags"))
        dep_ids = [x.strip() for x in (d.get("depends_on") or "").split(",") if x.strip()]
        d["blocked_by"] = [x for x in dep_ids if x in active_ids]
        result.append(d)
    return result


def fetch_project_items_all(label, db_path, cutoff_date_str):
    """ALL open items from a project DB; sets _surfaced=True for rollup-eligible rows."""
    conn = open_ro(db_path)
    cur  = conn.cursor()
    closed = ",".join("?" for _ in CLOSED_TAGS)
    has_xp    = _has_column(conn, "items", "xp_tags")
    has_recur = _has_column(conn, "items", "recur")
    has_deps  = _has_column(conn, "items", "depends_on")
    has_pri   = _has_column(conn, "items", "priority")
    has_wait  = _has_column(conn, "items", "wait_until")
    xp_col    = ", xp_tags"    if has_xp    else ""
    recur_col = ", recur"      if has_recur  else ""
    deps_col  = ", depends_on" if has_deps   else ""
    pri_col   = ", priority"   if has_pri    else ""
    wait_col  = ", wait_until" if has_wait   else ""
    cur.execute(f"""
        SELECT raw_id, section, title, owner, deadline, status_tag, status_detail,
               is_standing{xp_col}{recur_col}{deps_col}{pri_col}{wait_col}
        FROM items
        WHERE status_tag NOT IN ({closed}) AND is_standing = 0
        ORDER BY CASE WHEN deadline IS NULL THEN '9999' ELSE deadline END, sort_id
    """, tuple(CLOSED_TAGS))
    rows = cur.fetchall()
    cur.execute("SELECT raw_id FROM items")
    active_ids = {row[0] for row in cur.fetchall()}
    conn.close()
    today_iso = date.today().isoformat()
    result = []
    for r in rows:
        d = dict(r)
        title    = d.get("title", "") or ""
        deadline = d.get("deadline") or ""
        priority = d.get("priority") or ""
        wait     = d.get("wait_until") or ""
        xp_tags  = _parse_xp_tags(d.get("xp_tags"))
        dep_ids  = [x.strip() for x in (d.get("depends_on") or "").split(",") if x.strip()]
        d["_xp_tags"]   = xp_tags
        d["blocked_by"] = [x for x in dep_ids if x in active_ids]
        # Snoozed items are never surfaced regardless of other criteria
        snoozed = bool(wait and wait > today_iso)
        d["_surfaced"]  = (not snoozed) and (
            (XP_SENTINEL in title) or
            bool(deadline and deadline <= cutoff_date_str) or
            bool(xp_tags) or
            priority == 'H'
        )
        d["_project"]   = label
        d["_db"]        = str(db_path)
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

BANNER = (
    "<!-- AUTO-GENERATED by scripts/rollup.py — DO NOT EDIT DIRECTLY.\n"
    "     Regenerate: python3 scripts/rollup.py -->"
)

TABLE_HEADER = "| Deadline   | Project  | Item                                                      | Status          | Ref     |"
TABLE_SEP    = "|------------|----------|-----------------------------------------------------------|-----------------|---------|"


def _short(text, n):
    text = re.sub(r'\*\*', '', text or '').strip()
    return text[:n] + ('…' if len(text) > n else '')


def _ref(row):
    proj = row.get("_project", "")
    if proj == "Master":
        return f"priorities#{row['raw_id']}"
    return f"{proj}#{row['raw_id']}"


def build_projects_meta():
    """Return ordered metadata for the Add-task form: master first, then each project.

    Result: [{"label": str, "dir": str, "sections": [[slug, display], ...]}, ...]
    Projects that don't resolve on this machine are silently skipped (same graceful-
    degradation behaviour as the fetch path).
    """
    result = []

    # ── Master ────────────────────────────────────────────────────────────────
    master_sections = [[s, d] for s, d in getattr(_cfg, "SECTION_ORDER", [])] if _cfg else []
    result.append({
        "label":    "Master",
        "dir":      str(BASE),
        "sections": master_sections,
    })

    # ── Per-project ───────────────────────────────────────────────────────────
    for label, tail in PROJECTS:
        db_path = resolve_project_db(tail)
        if db_path is None:
            continue
        proj_dir = db_path.parent
        cfg_path = proj_dir / "scripts" / "tracker_config.py"
        if not cfg_path.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"_tc_{label}", cfg_path)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sections = [[s, d] for s, d in getattr(mod, "SECTION_ORDER", [])]
            result.append({
                "label":    label,
                "dir":      str(proj_dir),
                "sections": sections,
            })
        except Exception:
            continue

    return result


def render_md(all_items, window_days, generated_date):
    lines = [
        BANNER,
        "",
        f"# {PROJECT_TITLE} — Master Priorities",
        "",
        f"Generated: {generated_date}  |  Window: {window_days}-day deadline + `[XP]`-tagged items from project trackers.",
        "",
        "---",
        "",
        TABLE_HEADER,
        TABLE_SEP,
    ]
    for r in all_items:
        deadline   = r.get("deadline") or "—"
        proj       = r.get("_project", "")
        item       = _short(r.get("title", ""), 57)
        status     = _short(r.get("status_tag", ""), 15)
        ref        = _ref(r)
        lines.append(f"| {deadline:<10} | {proj:<8} | {item:<57} | {status:<15} | {ref:<7} |")
    footer = [
        "",
        "---",
        "",
        "**Legend:** `[XP]` = explicit cross-project priority (prepend to item title in project tracker to force inclusion regardless of deadline).",
        "",
    ]
    if PROJECTS:
        proj_labels = ", ".join(lbl for lbl, _ in PROJECTS)
        footer.append(f"Per-project trackers: {proj_labels}.")
    footer.append(f"Master tracker: `{BASE}`.")
    footer.append("")
    lines += footer
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --bg-page: #f5f5f7; --bg-header: rgba(255,255,255,0.95); --bg-filter: #f5f5f7;
      --bg-row-hover: #fff; --bg-detail: #fafafa; --bg-badge: #e8e8ed;
      --text-primary: #1d1d1f; --text-secondary: #6e6e73; --text-muted: #6e6e73; --text-badge: #3a3a3c;
      --border: #d2d2d7; --border-row: #e8e8ed;
      --btn-active-bg: #1d1d1f; --btn-active-text: #fff; --btn-active-border: #1d1d1f;
      --focus: #0071e3;
    }
    [data-theme="dark"] {
      --bg-page: #1c1c1e; --bg-header: rgba(28,28,30,0.97); --bg-filter: #1c1c1e;
      --bg-row-hover: #2c2c2e; --bg-detail: #2c2c2e; --bg-badge: #3a3a3c;
      --text-primary: #f5f5f7; --text-secondary: #aeaeb2; --text-muted: #8a8a8e; --text-badge: #d1d1d6;
      --border: #3a3a3c; --border-row: #2c2c2e;
      --btn-active-bg: #f5f5f7; --btn-active-text: #1c1c1e; --btn-active-border: #f5f5f7;
      --focus: #64b5f6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg-page); color: var(--text-primary); font-size: 14px; min-height: 100vh; }
    *:focus { outline: none; }
    *:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 3px; }

    .header {
      position: sticky; top: 0; z-index: 100;
      background: var(--bg-header); backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--border); padding: 12px 24px 10px;
    }
    .header-top { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    .header h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
    .header .meta { font-size: 12px; color: var(--text-secondary); flex: 1; }
    .header-actions { display: flex; gap: 6px; align-items: center; }
    .icon-btn {
      background: none; border: 1.5px solid var(--border); border-radius: 8px;
      padding: 4px 10px; font-size: 12px; color: var(--text-secondary);
      cursor: pointer; font-family: inherit; transition: all 0.12s; white-space: nowrap;
    }
    .icon-btn:hover { border-color: var(--text-primary); color: var(--text-primary); }
    .stats { display: flex; gap: 20px; margin-top: 8px; }
    .stat { font-size: 12px; color: var(--text-secondary); }
    .stat strong { font-size: 22px; font-weight: 600; display: block; color: var(--text-primary); line-height: 1.2; }
    .stat.urgent strong { color: #ff3b30; }
    .stat.soon strong { color: #ff9500; }

    .toolbar {
      padding: 8px 24px; display: flex; flex-wrap: wrap; gap: 7px;
      align-items: center; border-bottom: 1px solid var(--border); background: var(--bg-filter);
    }
    .filter-btn {
      padding: 5px 12px; border-radius: 20px; border: 1.5px solid var(--border);
      background: var(--bg-header); color: var(--text-primary); font-size: 12px; cursor: pointer;
      font-family: inherit; transition: background 0.12s, border-color 0.12s, color 0.12s;
      white-space: nowrap; display: inline-flex; align-items: center; gap: 3px;
    }
    .filter-btn:hover { border-color: var(--text-primary); }
    .filter-btn.active { background: var(--btn-active-bg); color: var(--btn-active-text); border-color: var(--btn-active-border); }
    /* Count badge — fixed min-width so changing digit count doesn't reflow the toolbar */
    .count-badge { display: inline-block; min-width: 2ch; text-align: right; opacity: 0.75; }
    .filter-sep { width: 1px; height: 22px; background: var(--border); margin: 0 2px; flex-shrink: 0; }
    .search-wrap { position: relative; flex: 1; min-width: 140px; max-width: 260px; }
    .search-wrap input {
      width: 100%; padding: 5px 26px 5px 10px; border-radius: 20px;
      border: 1.5px solid var(--border); background: var(--bg-header);
      color: var(--text-primary); font-size: 12px; font-family: inherit;
      transition: border-color 0.12s;
    }
    .search-wrap input::placeholder { color: var(--text-muted); }
    .search-wrap input:focus { border-color: var(--text-primary); }
    .search-clear {
      position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
      background: none; border: none; cursor: pointer; color: var(--text-muted); font-size: 13px;
      line-height: 1; padding: 0; display: none;
    }

    .status-bar {
      padding: 5px 24px; display: flex; flex-wrap: wrap; gap: 4px; align-items: center;
      border-bottom: 1px solid var(--border); background: var(--bg-filter);
    }
    .status-bar label {
      display: flex; align-items: center; gap: 4px; cursor: pointer;
      font-size: 12px; color: var(--text-secondary); padding: 3px 8px;
      border-radius: 12px; border: 1px solid transparent; transition: all 0.1s; white-space: nowrap;
    }
    .status-bar label:hover { border-color: var(--border); }
    .status-bar label.checked { background: var(--bg-badge); border-color: var(--border); color: var(--text-primary); font-weight: 500; }
    .status-bar input[type=checkbox] { width: 12px; height: 12px; cursor: pointer; accent-color: var(--text-primary); }
    .status-count { font-size: 10px; color: var(--text-muted); }

    .table-wrap { padding: 0 24px 40px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th {
      text-align: left; padding: 8px 10px; font-size: 11px; font-weight: 600;
      color: var(--text-secondary); border-bottom: 1.5px solid var(--border);
      text-transform: uppercase; letter-spacing: 0.06em; user-select: none;
    }
    th.sortable { cursor: pointer; }
    th.sortable:hover { color: var(--text-primary); }
    th.sort-active { color: var(--text-primary); }
    .sort-arrow { margin-left: 3px; font-size: 10px; opacity: 0.4; }
    th.sort-active .sort-arrow { opacity: 1; }
    tr.item-row { border-bottom: 1px solid var(--border-row); }
    tr.item-row.clickable { cursor: pointer; }
    tr.item-row:hover td { background: var(--bg-row-hover); }
    td { padding: 9px 10px; vertical-align: middle; }
    td.deadline-cell { width: 95px; font-variant-numeric: tabular-nums; font-size: 13px; padding-left: 12px; }
    tr.overdue  td.deadline-cell { border-left: 3px solid #ff3b30; padding-left: 9px; }
    tr.due-soon td.deadline-cell { border-left: 3px solid #ff9500; padding-left: 9px; }

    .proj-badge {
      display: inline-block; padding: 2px 7px; border-radius: 10px;
      font-size: 11px; font-weight: 600; background: var(--bg-badge); color: var(--text-badge); white-space: nowrap;
    }
    .xp-badge {
      display: inline-block; padding: 2px 6px; border-radius: 10px; margin-left: 4px;
      font-size: 10px; font-weight: 600; background: #e8f4fd; color: #0071e3;
      border: 1px solid #c7e0f4; white-space: nowrap;
    }
    [data-theme="dark"] .xp-badge { background: #1a3652; color: #64b5f6; border-color: #1e4976; }
    .recur-badge {
      display: inline-block; padding: 1px 5px; border-radius: 8px; margin-left: 4px;
      font-size: 10px; font-weight: 600; background: #e8f5e8; color: #2e7d32;
      border: 1px solid #b8d8b8; white-space: nowrap;
    }
    [data-theme="dark"] .recur-badge { background: #1a3021; color: #81c784; border-color: #2e5437; }
    .blocked-badge {
      display: inline-block; padding: 1px 5px; border-radius: 8px; margin-left: 4px;
      font-size: 10px; font-weight: 600; background: #fde8e8; color: #c62828;
      border: 1px solid #f5b8b8; white-space: nowrap;
    }
    [data-theme="dark"] .blocked-badge { background: #3d1a1a; color: #ef9a9a; border-color: #6d2222; }
    .pri-badge {
      display: inline-block; padding: 1px 5px; border-radius: 8px; margin-left: 4px;
      font-size: 10px; font-weight: 700; white-space: nowrap; border: 1px solid;
    }
    .pri-H { background: #fff0f0; color: #c62828; border-color: #f5b8b8; }
    .pri-M { background: #fff8e8; color: #b45309; border-color: #f5d898; }
    .pri-L { background: #f2f2f2; color: #555;    border-color: #d8d8d8; }
    [data-theme="dark"] .pri-H { background: #3d1a1a; color: #ef9a9a; border-color: #6d2222; }
    [data-theme="dark"] .pri-M { background: #3d2e10; color: #f6c468; border-color: #6d4f18; }
    [data-theme="dark"] .pri-L { background: #2a2a2a; color: #aaa;    border-color: #444; }
    .snooze-badge {
      display: inline-block; padding: 1px 5px; border-radius: 8px; margin-left: 4px;
      font-size: 10px; font-weight: 600; background: #f0f0ff; color: #5551cf;
      border: 1px solid #c8c6f5; white-space: nowrap;
    }
    [data-theme="dark"] .snooze-badge { background: #1e1e40; color: #9d9bf0; border-color: #3a387a; }
    .status-tag { font-size: 12px; color: var(--text-secondary); font-weight: 500; }
    .title-text { font-weight: 500; }
    .expand-icon {
      float: right; color: var(--text-muted); font-size: 13px; margin-left: 6px;
      transition: transform 0.18s; line-height: 1; display: inline-block;
    }
    tr.item-row.open .expand-icon { transform: rotate(180deg); }
    tr.detail-row { display: none; }
    tr.detail-row.open { display: table-row; }
    tr.detail-row td {
      padding: 6px 10px 14px 28px; font-size: 13px; color: var(--text-secondary);
      white-space: pre-wrap; background: var(--bg-detail); border-bottom: 1px solid var(--border-row);
    }
    tr.detail-row td a { color: var(--focus); }
    tr.detail-row td code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
      background: var(--bg-badge); padding: 0 4px; border-radius: 4px;
    }
    .ref-cell { font-size: 11px; color: var(--text-muted); white-space: nowrap; }
    tr.group-header { background: var(--bg-filter); }
    tr.group-header td {
      font-size: 11px; font-weight: 700; color: var(--text-secondary); padding: 5px 10px;
      letter-spacing: 0.06em; text-transform: uppercase; border-bottom: 1px solid var(--border);
    }
    .group-count { font-weight: 400; margin-left: 6px; color: var(--text-muted); }
    .no-results { text-align: center; color: var(--text-secondary); padding: 48px; font-size: 14px; }

    /* ── Edit affordance ─────────────────────────────────────────────────── */
    .title-cell { position: relative; }
    .edit-btn {
      display: none; position: absolute; right: 0; top: 50%; transform: translateY(-50%);
      padding: 2px 6px; font-size: 13px; cursor: pointer; color: var(--text-secondary);
      background: var(--bg-header); border: 1px solid var(--border); border-radius: 4px;
      line-height: 1; opacity: 0.8; user-select: none;
    }
    tr.item-row:hover .edit-btn { display: inline-block; }
    .edit-btn:hover { color: var(--text-primary); opacity: 1; border-color: var(--text-primary); }

    /* ── Add-task modal / Edit-task modal ────────────────────────────────── */
    .modal-overlay {
      display: none; position: fixed; inset: 0; z-index: 1000;
      background: rgba(0,0,0,0.45); align-items: center; justify-content: center;
    }
    .modal-overlay.open { display: flex; }
    .modal {
      background: var(--bg-header); border: 1px solid var(--border); border-radius: 14px;
      padding: 24px; min-width: 360px; max-width: 520px; width: 90%;
      box-shadow: 0 8px 32px rgba(0,0,0,0.22);
    }
    .modal h2 { font-size: 16px; font-weight: 700; margin-bottom: 16px; }
    .form-row { margin-bottom: 11px; }
    .form-row label {
      display: block; font-size: 11px; font-weight: 600; color: var(--text-secondary);
      text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px;
    }
    .form-row input, .form-row select {
      width: 100%; padding: 7px 10px; border-radius: 8px; border: 1.5px solid var(--border);
      background: var(--bg-page); color: var(--text-primary); font-size: 13px;
      font-family: inherit; transition: border-color 0.12s;
    }
    .form-row input:focus, .form-row select:focus { border-color: var(--text-primary); }
    .form-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
    .btn-primary {
      background: var(--btn-active-bg); color: var(--btn-active-text); border: none;
      border-radius: 8px; padding: 7px 18px; font-size: 13px; font-family: inherit;
      cursor: pointer; font-weight: 600;
    }
    .btn-cancel {
      background: none; border: 1.5px solid var(--border); border-radius: 8px;
      padding: 7px 14px; font-size: 13px; font-family: inherit; cursor: pointer;
      color: var(--text-secondary); transition: all 0.12s;
    }
    .btn-cancel:hover { border-color: var(--text-primary); color: var(--text-primary); }
    .add-result { margin-top: 10px; font-size: 12px; min-height: 1em; }
    .add-result.ok  { color: #34c759; }
    .add-result.err { color: #ff3b30; }
    .add-cmd-wrap { margin-top: 10px; }
    .add-cmd-wrap p { font-size: 11px; color: var(--text-secondary); margin-bottom: 4px; }
    .add-cmd-wrap pre {
      background: var(--bg-page); border: 1px solid var(--border); border-radius: 6px;
      padding: 8px; font-size: 11px; font-family: monospace; white-space: pre-wrap;
      word-break: break-all; color: var(--text-primary);
    }
    .copy-btn {
      margin-top: 4px; background: none; border: 1.5px solid var(--border);
      border-radius: 6px; padding: 3px 10px; font-size: 11px; cursor: pointer;
      color: var(--text-secondary); font-family: inherit; transition: all 0.12s;
    }
    .copy-btn:hover { border-color: var(--text-primary); color: var(--text-primary); }

    /* ── Responsive ──────────────────────────────────────────────────────────── */
    @media (max-width: 640px) {
      .header { padding-left: 12px; padding-right: 12px; }
      .toolbar { padding-left: 12px; padding-right: 12px; }
      .status-bar { padding-left: 12px; padding-right: 12px; }
      .table-wrap { padding-left: 12px; padding-right: 12px; padding-bottom: 32px; overflow-x: auto; }
      table { min-width: 540px; }
      .stats { flex-wrap: wrap; gap: 12px; }
      .modal { min-width: 0; width: calc(100% - 24px); }
      .search-wrap { min-width: 100px; }
    }

    /* ── Reduced motion ──────────────────────────────────────────────────────── */
    @media (prefers-reduced-motion: reduce) {
      * { transition: none !important; animation: none !important; }
    }
  </style>
</head>
<body>
<!-- AUTO-GENERATED by scripts/rollup.py --html — DO NOT EDIT DIRECTLY.
     Regenerate: cd <tracker-root> && python3 scripts/rollup.py --html -->
<div class="header">
  <div class="header-top">
    <h1>__TITLE__</h1>
    <div class="meta">Generated __GENERATED__ &middot; __WINDOW_DAYS__-day window</div>
    <div class="header-actions">
      <button class="icon-btn" id="add-btn">+ Add</button>
      <button class="icon-btn" id="group-btn">Group: <span id="group-label">None</span></button>
      <button class="icon-btn" id="theme-btn" aria-label="Toggle dark/light theme">🌙</button>
    </div>
  </div>
  <div class="stats" id="stats-bar"></div>
</div>
<div class="toolbar" id="toolbar"></div>
<div class="status-bar" id="status-bar"></div>
<div class="table-wrap">
  <table id="main-table">
    <thead><tr>
      <th class="sortable sort-active" data-sort="deadline" style="width:95px">Deadline <span class="sort-arrow">↑</span></th>
      <th class="sortable" data-sort="project" style="width:90px">Project <span class="sort-arrow"></span></th>
      <th class="sortable" data-sort="title">Item <span class="sort-arrow"></span></th>
      <th class="sortable" data-sort="priority" style="width:60px">Pri <span class="sort-arrow"></span></th>
      <th class="sortable" data-sort="status" style="width:130px">Status <span class="sort-arrow"></span></th>
      <th style="width:90px">Ref</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="no-results" id="no-results" style="display:none">No items match this filter.</div>
</div>

<div class="modal-overlay" id="add-overlay">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="add-modal-title">
    <h2 id="add-modal-title">+ Add Task</h2>
    <div class="form-row">
      <label for="add-project">Project</label>
      <select id="add-project"></select>
    </div>
    <div class="form-row">
      <label for="add-section">Section</label>
      <select id="add-section"></select>
    </div>
    <div class="form-row">
      <label for="add-title">Title *</label>
      <input type="text" id="add-title" placeholder="Task description">
    </div>
    <div class="form-row">
      <label for="add-owner">Owner</label>
      <input type="text" id="add-owner" placeholder="Optional">
    </div>
    <div class="form-row">
      <label for="add-deadline">Deadline</label>
      <input type="date" id="add-deadline">
    </div>
    <div class="form-row">
      <label for="add-xp">Cross-project tags</label>
      <input type="text" id="add-xp" placeholder="e.g. ProjectA,ProjectB (optional)">
    </div>
    <div class="form-row">
      <label for="add-recur">Recurrence</label>
      <input type="text" id="add-recur" placeholder="e.g. monthly, weekly, 2w (optional; requires deadline)">
    </div>
    <div class="form-row">
      <label for="add-depends">Depends on</label>
      <input type="text" id="add-depends" placeholder="e.g. 8,12 — item IDs that must close first (optional)">
    </div>
    <div class="form-row">
      <label for="add-priority">Priority</label>
      <select id="add-priority">
        <option value="">— none —</option>
        <option value="H">H — High</option>
        <option value="M">M — Medium</option>
        <option value="L">L — Low</option>
      </select>
    </div>
    <div class="form-row">
      <label for="add-snooze">Snooze until</label>
      <input type="date" id="add-snooze" placeholder="Hide from list until this date (optional)">
    </div>
    <div class="form-actions">
      <button class="btn-cancel" id="add-cancel">Cancel</button>
      <button class="btn-primary" id="add-submit">Add Task</button>
    </div>
    <div class="add-result" id="add-result"></div>
    <div class="add-cmd-wrap" id="add-cmd-wrap" style="display:none">
      <p>Server not running — copy this command and run it on the host machine:</p>
      <pre id="add-cmd"></pre>
      <button class="copy-btn" id="add-copy">Copy</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="edit-overlay">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="edit-modal-title">
    <h2 id="edit-modal-title">✎ Edit Task</h2>
    <div class="form-row">
      <label for="edit-project">Project</label>
      <input type="text" id="edit-project" disabled style="opacity:0.6;cursor:not-allowed">
    </div>
    <div class="form-row">
      <label for="edit-section">Section</label>
      <select id="edit-section"></select>
    </div>
    <div class="form-row">
      <label for="edit-title">Title *</label>
      <input type="text" id="edit-title" placeholder="Task description">
    </div>
    <div class="form-row">
      <label for="edit-owner">Owner</label>
      <input type="text" id="edit-owner" placeholder="Optional">
    </div>
    <div class="form-row">
      <label for="edit-deadline">Deadline</label>
      <input type="date" id="edit-deadline">
    </div>
    <div class="form-row">
      <label for="edit-status-tag">Status tag</label>
      <input type="text" id="edit-status-tag" placeholder="e.g. OPEN, IN PROGRESS, DONE">
    </div>
    <div class="form-row">
      <label for="edit-xp">Cross-project tags</label>
      <input type="text" id="edit-xp" placeholder="e.g. ProjectA,ProjectB (optional)">
    </div>
    <div class="form-row">
      <label for="edit-recur">Recurrence</label>
      <input type="text" id="edit-recur" placeholder="e.g. monthly, weekly, 2w (blank to clear)">
    </div>
    <div class="form-row">
      <label for="edit-depends">Depends on</label>
      <input type="text" id="edit-depends" placeholder="e.g. 8,12 (blank to clear)">
    </div>
    <div class="form-row">
      <label for="edit-priority">Priority</label>
      <select id="edit-priority">
        <option value="">— none —</option>
        <option value="H">H — High</option>
        <option value="M">M — Medium</option>
        <option value="L">L — Low</option>
      </select>
    </div>
    <div class="form-row">
      <label for="edit-snooze">Snooze until</label>
      <input type="date" id="edit-snooze" placeholder="Hide until this date (blank to clear)">
    </div>
    <div class="form-actions" style="flex-wrap:wrap;gap:6px">
      <button class="btn-cancel" id="edit-cancel">Cancel</button>
      <button class="btn-primary" id="edit-save">Save</button>
      <button class="btn-primary" id="edit-done" style="background:#34c759;border-color:#34c759">Done</button>
      <button class="btn-primary" id="edit-archive" style="background:#636366;border-color:#636366">Archive</button>
    </div>
    <div class="add-result" id="edit-result"></div>
    <div class="add-cmd-wrap" id="edit-cmd-wrap" style="display:none">
      <p>Server not running — copy this command and run it on the host machine:</p>
      <pre id="edit-cmd"></pre>
      <button class="copy-btn" id="edit-copy">Copy</button>
    </div>
  </div>
</div>

<script type="application/json" id="items-data">
__DATA_JSON__
</script>
<script type="application/json" id="projects-meta">
__PROJECTS_META_JSON__
</script>

<script>
(function () {
  var TODAY  = "__TODAY_ISO__";
  var WINDOW = __WINDOW_DAYS_INT__;
  var STORE_KEY = 'cp-tracker-v1';

  var items = JSON.parse(document.getElementById('items-data').textContent);

  // ── State ──────────────────────────────────────────────────────────────────
  var state = {
    view: 'surfaced',
    projects: new Set(),
    statuses: new Set(),
    search: '',
    group: 'none',
    sort: { key: 'deadline', dir: 1 },
    theme: 'light'
  };
  var GROUPS       = ['none', 'project', 'status', 'due'];
  var GROUP_LABELS = { none: 'None', project: 'Project', status: 'Status', due: 'Due bucket' };

  // ── Persistence ────────────────────────────────────────────────────────────
  function saveState() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        view: state.view, projects: Array.from(state.projects),
        statuses: Array.from(state.statuses), search: state.search,
        group: state.group, sort: state.sort, theme: state.theme
      }));
    } catch(e) {}
    var h = [];
    if (state.view !== 'surfaced') h.push('view=' + state.view);
    if (state.projects.size) h.push('proj=' + Array.from(state.projects).join(','));
    if (state.statuses.size) h.push('st=' + Array.from(state.statuses).join(','));
    if (state.search) h.push('q=' + encodeURIComponent(state.search));
    if (state.group !== 'none') h.push('g=' + state.group);
    if (state.sort.key !== 'deadline' || state.sort.dir !== 1)
      h.push('sort=' + state.sort.key + ':' + state.sort.dir);
    history.replaceState(null, '', h.length ? '#' + h.join('&') : window.location.pathname + window.location.search);
  }

  function loadState() {
    var from = {}, hash = window.location.hash.slice(1);
    if (hash) {
      hash.split('&').forEach(function(part) {
        var eq = part.indexOf('=');
        if (eq > 0) from[part.slice(0, eq)] = decodeURIComponent(part.slice(eq + 1));
      });
    } else {
      try { from = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch(e) {}
    }
    if (from.view) state.view = from.view;
    var pa = from.projects ? (Array.isArray(from.projects) ? from.projects : from.projects.split(',')) : (from.proj ? from.proj.split(',') : []);
    pa.forEach(function(p) { if (p) state.projects.add(p); });
    var sa = from.statuses ? (Array.isArray(from.statuses) ? from.statuses : from.statuses.split(',')) : (from.st ? from.st.split(',') : []);
    sa.forEach(function(s) { if (s) state.statuses.add(s); });
    if (from.search || from.q) state.search = from.search || from.q;
    if (from.group || from.g) state.group = from.group || from.g;
    if (from.sort) {
      if (typeof from.sort === 'object') { state.sort.key = from.sort.key || 'deadline'; state.sort.dir = from.sort.dir || 1; }
      else { var sp = from.sort.split(':'); state.sort.key = sp[0] || 'deadline'; state.sort.dir = parseInt(sp[1], 10) || 1; }
    }
    // Theme lives only in localStorage (never in the URL hash), so read it
    // regardless of which branch populated `from` — otherwise opening a link
    // with a hash silently resets the theme.
    try {
      var ls = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
      if (ls.theme) state.theme = ls.theme;
    } catch(e) {}
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function dlClass(deadline) {
    if (!deadline) return '';
    if (deadline < TODAY) return 'overdue';
    var cutoff = new Date(TODAY + 'T00:00:00');
    cutoff.setDate(cutoff.getDate() + WINDOW);
    return new Date(deadline + 'T00:00:00') <= cutoff ? 'due-soon' : '';
  }

  function refLabel(item) {
    return (item._project === 'Master' ? 'priorities' : item._project) + '#' + item.raw_id;
  }

  function esc(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Minimal safe markdown for status text: escape first, then render
  // [text](http…) links, **bold**, and `code`. Newlines are handled by the
  // detail cell's white-space: pre-wrap.
  function mdLite(s) {
    var h = esc(s);
    h = h.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)/g,
                  '<a href="$2" target="_blank" rel="noopener">$1</a>');
    h = h.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    return h;
  }

  function itemInProjects(item, projs) {
    if (!projs.size) return true;
    if (projs.has(item._project)) return true;
    var xp = item._xp_tags;
    if (xp && xp.length) { for (var i = 0; i < xp.length; i++) { if (projs.has(xp[i])) return true; } }
    return false;
  }

  function getFiltered(overProj, overSt, overView) {
    var p = overProj  !== undefined ? overProj  : state.projects;
    var s = overSt    !== undefined ? overSt    : state.statuses;
    var v = overView  !== undefined ? overView  : state.view;
    var q = state.search ? state.search.toLowerCase() : '';
    // A query containing '#' is a ref lookup (e.g. "DESC#12") — search across
    // every view so the ref is found even when snoozed or outside the window.
    var refQ = q.indexOf('#') >= 0;
    return items.filter(function(item) {
      // view
      var vOk;
      switch (v) {
        case 'surfaced': vOk = !!item._surfaced; break;
        case 'overdue':  vOk = !!(item.deadline && item.deadline < TODAY); break;
        case 'due-soon': vOk = dlClass(item.deadline) === 'due-soon'; break;
        case 'blocked':  vOk = !!(item.blocked_by && item.blocked_by.length); break;
        case 'snoozed':  vOk = !!(item.wait_until && item.wait_until > TODAY); break;
        default:         vOk = true;
      }
      if (!vOk && !refQ) return false;
      // project (multi, OR)
      if (!itemInProjects(item, p)) return false;
      // status (multi, OR)
      if (s.size && !s.has(item.status_tag || 'OPEN')) return false;
      // search (title, status, notes, and the cross-project ref label)
      if (q && (item.title || '').toLowerCase().indexOf(q) < 0 &&
               (item.status_tag || '').toLowerCase().indexOf(q) < 0 &&
               (item.status_detail || '').toLowerCase().indexOf(q) < 0 &&
               refLabel(item).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  function priRank(p) { return p==='H'?0:p==='M'?1:p==='L'?2:3; }

  function sortItems(list) {
    var key = state.sort.key, dir = state.sort.dir;
    return list.slice().sort(function(a, b) {
      var va, vb;
      if (key === 'deadline')  { va = a.deadline || '9999-99-99'; vb = b.deadline || '9999-99-99'; }
      else if (key === 'project')  { va = a._project || ''; vb = b._project || ''; }
      else if (key === 'status')   { va = a.status_tag || ''; vb = b.status_tag || ''; }
      else if (key === 'priority') { va = priRank(a.priority); vb = priRank(b.priority);
        if (va !== vb) return (va - vb) * dir; }
      else { va = (a.title||'').replace(/^\\[XP[^\\]]*\\]\\s*/i,'').toLowerCase(); vb = (b.title||'').replace(/^\\[XP[^\\]]*\\]\\s*/i,'').toLowerCase(); }
      if (typeof va === 'string') { if (va < vb) return -dir; if (va > vb) return dir; }
      // tiebreakers: priority rank, then deadline
      var pa = priRank(a.priority), pb = priRank(b.priority);
      if (pa !== pb) return pa - pb;
      var da = a.deadline||'9999-99-99', db = b.deadline||'9999-99-99';
      if (da !== db) return da < db ? -1 : 1;
      return 0;
    });
  }

  function groupItems(list) {
    if (state.group === 'none') return [{ label: null, items: list }];
    var buckets = {}, order = [];
    list.forEach(function(item) {
      var k;
      if (state.group === 'project') k = item._project || 'Other';
      else if (state.group === 'status') k = item.status_tag || 'OPEN';
      else { var dc = dlClass(item.deadline); k = dc === 'overdue' ? '🔴 Overdue' : dc === 'due-soon' ? '🟡 Due Soon' : '📋 Later / No Date'; }
      if (!buckets[k]) { buckets[k] = []; order.push(k); }
      buckets[k].push(item);
    });
    return order.map(function(k) { return { label: k, items: buckets[k] }; });
  }

  // ── Project + status inventory ─────────────────────────────────────────────
  var projSeen = {}, projs = [];
  items.forEach(function(i) {
    if (!projSeen[i._project]) { projSeen[i._project] = 1; projs.push(i._project); }
    if (i._xp_tags) i._xp_tags.forEach(function(p) { if (!projSeen[p]) { projSeen[p] = 1; projs.push(p); } });
  });
  projs.sort(function(a, b) { if (a==='Master') return -1; if (b==='Master') return 1; return a<b?-1:1; });

  var statusSeen = {}, allStatuses = [];
  items.forEach(function(i) { var s = i.status_tag||'OPEN'; if (!statusSeen[s]) { statusSeen[s]=1; allStatuses.push(s); } });
  allStatuses.sort();

  // ── Stats bar ─────────────────────────────────────────────────────────────
  var surfaced = items.filter(function(i) { return i._surfaced; });
  var nOver = surfaced.filter(function(i) { return i.deadline && i.deadline < TODAY; }).length;
  var nSoon = surfaced.filter(function(i) { return dlClass(i.deadline) === 'due-soon'; }).length;
  document.getElementById('stats-bar').innerHTML =
    '<div class="stat"><strong>' + surfaced.length + '</strong>surfaced</div>' +
    '<div class="stat urgent"><strong>' + nOver + '</strong>overdue</div>' +
    '<div class="stat soon"><strong>' + nSoon + '</strong>due soon</div>' +
    '<div class="stat"><strong>' + items.length + '</strong>total open</div>';

  // ── Build toolbar ─────────────────────────────────────────────────────────
  var toolbar = document.getElementById('toolbar');

  // View tabs
  var VIEW_DEFS = [{id:'surfaced',label:'Surfaced'},{id:'overdue',label:'Overdue'},{id:'due-soon',label:'Due Soon'},{id:'blocked',label:'Blocked'},{id:'snoozed',label:'Snoozed'},{id:'all',label:'All'}];
  VIEW_DEFS.forEach(function(f) {
    var btn = document.createElement('button');
    btn.className = 'filter-btn'; btn.setAttribute('data-view', f.id);
    btn.onclick = function() { state.view = f.id; render(); };
    toolbar.appendChild(btn);
  });

  var sep1 = document.createElement('div'); sep1.className = 'filter-sep'; toolbar.appendChild(sep1);

  // Project chips (multi-select toggle)
  projs.forEach(function(proj) {
    var btn = document.createElement('button');
    btn.className = 'filter-btn'; btn.setAttribute('data-proj', proj);
    btn.onclick = function() {
      if (state.projects.has(proj)) state.projects.delete(proj); else state.projects.add(proj);
      render();
    };
    toolbar.appendChild(btn);
  });

  var sep2 = document.createElement('div'); sep2.className = 'filter-sep'; toolbar.appendChild(sep2);

  // Search
  var sw = document.createElement('div'); sw.className = 'search-wrap';
  var si = document.createElement('input'); si.type='text'; si.id='search-input'; si.placeholder='Search text or ref…';
  var sc = document.createElement('button'); sc.className='search-clear'; sc.textContent='✕'; sc.title='Clear'; sc.setAttribute('aria-label', 'Clear search');
  sw.appendChild(si); sw.appendChild(sc); toolbar.appendChild(sw);
  si.oninput = function() { state.search = this.value; sc.style.display = this.value?'block':'none'; render(); };
  sc.onclick  = function() { state.search=''; si.value=''; sc.style.display='none'; render(); };

  // Status checkboxes
  var statusBar = document.getElementById('status-bar');
  allStatuses.forEach(function(st) {
    var lbl = document.createElement('label'); lbl.setAttribute('data-st-label', st);
    var cb = document.createElement('input'); cb.type='checkbox'; cb.value=st;
    cb.onchange = function() { if (this.checked) state.statuses.add(st); else state.statuses.delete(st); render(); };
    var span = document.createElement('span'); span.textContent = st;
    var cnt  = document.createElement('span'); cnt.className='status-count'; cnt.setAttribute('data-st-count', st);
    lbl.appendChild(cb); lbl.appendChild(span); lbl.appendChild(cnt);
    statusBar.appendChild(lbl);
  });

  // Group button
  var groupBtn = document.getElementById('group-btn');
  var groupLabel = document.getElementById('group-label');
  groupBtn.onclick = function() {
    var idx = GROUPS.indexOf(state.group);
    state.group = GROUPS[(idx + 1) % GROUPS.length];
    groupLabel.textContent = GROUP_LABELS[state.group];
    render();
  };

  // Theme button
  var themeBtn = document.getElementById('theme-btn');
  function applyTheme() {
    document.documentElement.setAttribute('data-theme', state.theme);
    themeBtn.textContent = state.theme === 'dark' ? '☀️' : '🌙';
  }
  themeBtn.onclick = function() { state.theme = state.theme==='dark'?'light':'dark'; applyTheme(); saveState(); };

  // Sortable column headers
  document.querySelectorAll('th[data-sort]').forEach(function(th) {
    th.addEventListener('click', function() {
      var key = th.getAttribute('data-sort');
      if (state.sort.key === key) state.sort.dir = -state.sort.dir;
      else { state.sort.key = key; state.sort.dir = 1; }
      render();
    });
  });

  function updateSortArrows() {
    document.querySelectorAll('th[data-sort]').forEach(function(th) {
      var key = th.getAttribute('data-sort');
      var arrow = th.querySelector('.sort-arrow');
      th.classList.toggle('sort-active', key === state.sort.key);
      arrow.textContent = key === state.sort.key ? (state.sort.dir===1 ? '↑' : '↓') : '';
    });
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  function render() {
    var filtered = getFiltered();
    var sorted   = sortItems(filtered);
    var grouped  = groupItems(sorted);

    // View tab counts + active state
    toolbar.querySelectorAll('[data-view]').forEach(function(btn) {
      var vid = btn.getAttribute('data-view');
      var cnt = getFiltered(undefined, undefined, vid).length;
      var labels = {surfaced:'Surfaced',overdue:'Overdue','due-soon':'Due Soon',blocked:'Blocked',snoozed:'Snoozed',all:'All'};
      btn.innerHTML = esc(labels[vid]||vid) + ' <span class="count-badge">(' + cnt + ')</span>';
      btn.classList.toggle('active', state.view === vid);
    });

    // Project chip counts + active state
    toolbar.querySelectorAll('[data-proj]').forEach(function(btn) {
      var proj = btn.getAttribute('data-proj');
      var without = new Set(state.projects); without.delete(proj);
      var cnt = getFiltered(without, undefined, undefined).filter(function(i) {
        return i._project===proj || (i._xp_tags && i._xp_tags.indexOf(proj)>=0);
      }).length;
      btn.innerHTML = esc(proj) + ' <span class="count-badge">(' + cnt + ')</span>';
      btn.classList.toggle('active', state.projects.has(proj));
    });

    // Status checkbox counts + checked state
    allStatuses.forEach(function(st) {
      var without = new Set(state.statuses); without.delete(st);
      var cnt = getFiltered(undefined, without, undefined).filter(function(i){ return (i.status_tag||'OPEN')===st; }).length;
      var cntEl = statusBar.querySelector('[data-st-count="' + st + '"]');
      if (cntEl) cntEl.textContent = ' (' + cnt + ')';
      var lbl = statusBar.querySelector('[data-st-label="' + st + '"]');
      if (lbl) { var cb = lbl.querySelector('input'); cb.checked = state.statuses.has(st); lbl.classList.toggle('checked', state.statuses.has(st)); }
    });

    groupLabel.textContent = GROUP_LABELS[state.group];
    updateSortArrows();

    var tbody = document.getElementById('tbody');
    var noRes = document.getElementById('no-results');
    tbody.innerHTML = '';

    if (!sorted.length) { noRes.style.display = ''; saveState(); return; }
    noRes.style.display = 'none';

    var rowIdx = 0;
    grouped.forEach(function(grp) {
      if (grp.label !== null) {
        var ghdr = document.createElement('tr'); ghdr.className = 'group-header';
        ghdr.innerHTML = '<td colspan="6">' + esc(grp.label) + '<span class="group-count">(' + grp.items.length + ')</span></td>';
        tbody.appendChild(ghdr);
      }
      grp.items.forEach(function(item) {
        var dc = dlClass(item.deadline), dl = item.deadline || '—', ref = refLabel(item);
        var hasDetail = !!(item.status_detail && item.status_detail.trim());
        var detailId = 'detail-' + rowIdx;

        var xpHtml = '';
        if (item._xp_tags && item._xp_tags.length) {
          item._xp_tags.forEach(function(xp) { xpHtml += '<span class="xp-badge">↗ ' + esc(xp) + '</span>'; });
        }

        var recurHtml = '';
        if (item.recur) {
          recurHtml = '<span class="recur-badge" title="Recurs: ' + esc(item.recur) + '">&#x1F501; ' + esc(item.recur) + '</span>';
        }

        var blockedHtml = '';
        if (item.blocked_by && item.blocked_by.length) {
          var blkIds = item.blocked_by.map(function(id) { return '#' + esc(id); }).join(', ');
          blockedHtml = '<span class="blocked-badge" title="Blocked by: ' + blkIds + '">&#x1F512; ' + blkIds + '</span>';
        }

        var snoozeHtml = '';
        if (item.wait_until) {
          snoozeHtml = '<span class="snooze-badge" title="Snoozed until ' + esc(item.wait_until) + '">&#x1F4A4; ' + esc(item.wait_until) + '</span>';
        }

        var priHtml = '';
        if (item.priority) {
          priHtml = '<span class="pri-badge pri-' + esc(item.priority) + '">' + esc(item.priority) + '</span>';
        }

        var tr = document.createElement('tr');
        tr.className = 'item-row' + (dc ? ' '+dc : '') + (hasDetail ? ' clickable' : '');
        if (hasDetail) { tr.setAttribute('tabindex', '0'); tr.setAttribute('aria-expanded', 'false'); }
        tr.innerHTML =
          '<td class="deadline-cell">' + esc(dl) + '</td>' +
          '<td><span class="proj-badge">' + esc(item._project) + '</span>' + xpHtml + '</td>' +
          '<td class="title-cell"><span class="title-text">' + esc(item.title||'') + '</span>' +
            recurHtml + blockedHtml + snoozeHtml +
            (hasDetail ? '<span class="expand-icon" aria-hidden="true">&#9660;</span>' : '') +
            '<span class="edit-btn" role="button" tabindex="0" aria-label="Edit task" title="Edit task">✎</span>' + '</td>' +
          '<td>' + priHtml + '</td>' +
          '<td><span class="status-tag">' + esc(item.status_tag||'') + '</span></td>' +
          '<td class="ref-cell">' + esc(ref) + '</td>';

        if (hasDetail) {
          var toggleDetail = (function(row, did) {
            return function() {
              row.classList.toggle('open');
              var dr = document.getElementById(did);
              if (dr) dr.classList.toggle('open');
              row.setAttribute('aria-expanded', row.classList.contains('open') ? 'true' : 'false');
            };
          })(tr, detailId);
          tr.onclick = toggleDetail;
          tr.onkeydown = function(e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleDetail(); } };
        }

        // Edit button — stop propagation so it doesn't toggle the detail row
        (function(it) {
          var eb = tr.querySelector('.edit-btn');
          if (eb) {
            eb.onclick = function(e) { e.stopPropagation(); openEdit(it); };
            eb.onkeydown = function(e) { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); e.preventDefault(); openEdit(it); } };
          }
        })(item);
        tbody.appendChild(tr);

        if (hasDetail) {
          var dr = document.createElement('tr'); dr.className='detail-row'; dr.id=detailId;
          dr.innerHTML = '<td colspan="6">' + mdLite(item.status_detail) + '</td>';
          tbody.appendChild(dr);
        }
        rowIdx++;
      });
    });
    saveState();
  }

  // ── Add-task modal ────────────────────────────────────────────────────────
  var PROJECTS_META = JSON.parse(document.getElementById('projects-meta').textContent);
  var SERVED = (location.protocol === 'http:' || location.protocol === 'https:');

  var addOverlay   = document.getElementById('add-overlay');
  var addResult    = document.getElementById('add-result');
  var addCmdWrap   = document.getElementById('add-cmd-wrap');
  var addCmd       = document.getElementById('add-cmd');
  var addProjectSel = document.getElementById('add-project');
  var addSectionSel = document.getElementById('add-section');

  // Populate project select
  PROJECTS_META.forEach(function(p) {
    var opt = document.createElement('option');
    opt.value = p.label; opt.textContent = p.label;
    addProjectSel.appendChild(opt);
  });

  function rebuildSections() {
    var label = addProjectSel.value;
    var meta = null;
    for (var i = 0; i < PROJECTS_META.length; i++) { if (PROJECTS_META[i].label === label) { meta = PROJECTS_META[i]; break; } }
    addSectionSel.innerHTML = '';
    if (meta) {
      meta.sections.forEach(function(sec) {
        var opt = document.createElement('option');
        opt.value = sec[0]; opt.textContent = sec[1];
        addSectionSel.appendChild(opt);
      });
    }
  }
  addProjectSel.onchange = rebuildSections;
  rebuildSections();

  function resetModal() {
    document.getElementById('add-title').value = '';
    document.getElementById('add-owner').value = '';
    document.getElementById('add-deadline').value = '';
    document.getElementById('add-xp').value = '';
    document.getElementById('add-recur').value = '';
    document.getElementById('add-depends').value = '';
    document.getElementById('add-priority').value = '';
    document.getElementById('add-snooze').value = '';
    addResult.textContent = ''; addResult.className = 'add-result';
    addCmdWrap.style.display = 'none';
  }

  var _addOpener = null;
  function closeAdd() { addOverlay.classList.remove('open'); if (_addOpener) { _addOpener.focus(); _addOpener = null; } }

  document.getElementById('add-btn').onclick = function() {
    _addOpener = this;
    resetModal();
    addOverlay.classList.add('open');
    document.getElementById('add-title').focus();
  };

  document.getElementById('add-cancel').onclick = closeAdd;

  addOverlay.onclick = function(e) {
    if (e.target === addOverlay) closeAdd();
  };

  function buildCmd(payload) {
    var meta = null;
    for (var i = 0; i < PROJECTS_META.length; i++) { if (PROJECTS_META[i].label === payload.project) { meta = PROJECTS_META[i]; break; } }
    var dir = meta ? meta.dir : '.';
    var cmd = '';
    if (payload.project !== 'Master') cmd += 'cd ' + dir + ' && ';
    cmd += 'python3 scripts/todo.py add';
    cmd += ' --section ' + JSON.stringify(payload.section);
    cmd += ' --title ' + JSON.stringify(payload.title);
    if (payload.owner)      cmd += ' --owner '   + JSON.stringify(payload.owner);
    if (payload.deadline)   cmd += ' --deadline ' + JSON.stringify(payload.deadline);
    if (payload.xp_tags)    cmd += ' --xp '       + JSON.stringify(payload.xp_tags);
    if (payload.recur)      cmd += ' --recur '    + JSON.stringify(payload.recur);
    if (payload.depends_on) cmd += ' --depends '  + JSON.stringify(payload.depends_on);
    if (payload.priority)   cmd += ' --priority ' + JSON.stringify(payload.priority);
    if (payload.wait_until) cmd += ' --snooze '   + JSON.stringify(payload.wait_until);
    return cmd;
  }

  document.getElementById('add-submit').onclick = function() {
    var title = document.getElementById('add-title').value.trim();
    if (!title) {
      addResult.textContent = 'Title is required.'; addResult.className = 'add-result err';
      return;
    }
    var payload = {
      project:  addProjectSel.value,
      section:  addSectionSel.value,
      title:    title,
      owner:      document.getElementById('add-owner').value.trim() || null,
      deadline:   document.getElementById('add-deadline').value || null,
      xp_tags:    document.getElementById('add-xp').value.trim() || null,
      recur:      document.getElementById('add-recur').value.trim() || null,
      depends_on: document.getElementById('add-depends').value.trim() || null,
      priority:   document.getElementById('add-priority').value || null,
      wait_until: document.getElementById('add-snooze').value || null,
    };

    addCmdWrap.style.display = 'none';

    if (SERVED) {
      addResult.textContent = 'Saving…'; addResult.className = 'add-result';
      fetch('/api/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Tracker': '1'},
        body: JSON.stringify(payload)
      })
        .then(function(r) { return r.json(); })
        .then(function(j) {
          if (j.ok) {
            addResult.textContent = '✅ Added. Refreshing…';
            addResult.className = 'add-result ok';
            setTimeout(function() { location.reload(); }, 900);
          } else {
            addResult.textContent = '❌ ' + (j.error || 'Unknown error');
            addResult.className = 'add-result err';
          }
        })
        .catch(function() {
          addResult.textContent = 'Server unreachable — copy the command:';
          addResult.className = 'add-result err';
          addCmd.textContent = buildCmd(payload);
          addCmdWrap.style.display = '';
        });
    } else {
      addCmd.textContent = buildCmd(payload);
      addCmdWrap.style.display = '';
      addResult.textContent = '';
    }
  };

  document.getElementById('add-copy').onclick = function() {
    var btn = document.getElementById('add-copy');
    navigator.clipboard.writeText(addCmd.textContent).then(function() {
      btn.textContent = 'Copied!';
      setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
    });
  };

  // ── Edit-task modal ────────────────────────────────────────────────────────
  var editOverlay  = document.getElementById('edit-overlay');
  var editResult   = document.getElementById('edit-result');
  var editCmdWrap  = document.getElementById('edit-cmd-wrap');
  var editCmd      = document.getElementById('edit-cmd');
  var editSection  = document.getElementById('edit-section');

  // Currently-edited item state (set by openEdit)
  var _editItem = null;

  function openEdit(item) {
    _editItem = item;
    document.getElementById('edit-project').value    = item._project || '';
    document.getElementById('edit-title').value      = item.title    || '';
    document.getElementById('edit-owner').value      = item.owner    || '';
    document.getElementById('edit-deadline').value   = item.deadline || '';
    document.getElementById('edit-status-tag').value = item.status_tag || '';
    document.getElementById('edit-xp').value         = (item._xp_tags||[]).join(',');
    document.getElementById('edit-recur').value      = item.recur      || '';
    document.getElementById('edit-depends').value    = item.depends_on  || '';
    document.getElementById('edit-priority').value   = item.priority    || '';
    document.getElementById('edit-snooze').value     = item.wait_until  || '';
    editResult.textContent = ''; editResult.className = 'add-result';
    editCmdWrap.style.display = 'none';

    // Populate section dropdown from PROJECTS_META for this project
    editSection.innerHTML = '';
    var meta = null;
    for (var i = 0; i < PROJECTS_META.length; i++) {
      if (PROJECTS_META[i].label === item._project) { meta = PROJECTS_META[i]; break; }
    }
    if (meta) {
      meta.sections.forEach(function(sec) {
        var opt = document.createElement('option');
        opt.value = sec[0]; opt.textContent = sec[1];
        if (sec[0] === item.section) opt.selected = true;
        editSection.appendChild(opt);
      });
    }
    _editOpener = document.activeElement;
    editOverlay.classList.add('open');
    document.getElementById('edit-title').focus();
  }

  var _editOpener = null;
  function closeEdit() { editOverlay.classList.remove('open'); _editItem = null; if (_editOpener) { _editOpener.focus(); _editOpener = null; } }

  document.getElementById('edit-cancel').onclick = closeEdit;
  editOverlay.onclick = function(e) { if (e.target === editOverlay) closeEdit(); };

  function buildEditCmd(payload) {
    var meta = null;
    for (var i = 0; i < PROJECTS_META.length; i++) {
      if (PROJECTS_META[i].label === payload.project) { meta = PROJECTS_META[i]; break; }
    }
    var dir = meta ? meta.dir : '.';
    var prefix = (payload.project !== 'Master') ? 'cd ' + dir + ' && ' : '';
    var cmd = prefix + 'python3 scripts/todo.py update ' + payload.id;
    if (payload.title      !== undefined) cmd += ' --title '   + JSON.stringify(payload.title);
    if (payload.owner      !== undefined) cmd += ' --owner '   + JSON.stringify(payload.owner || '');
    if (payload.deadline   !== undefined) cmd += ' --deadline ' + JSON.stringify(payload.deadline || '');
    if (payload.section    !== undefined) cmd += ' --section '  + JSON.stringify(payload.section || '');
    if (payload.status_tag !== undefined && payload.status_tag !== null)
                                          cmd += ' --tag '     + JSON.stringify(payload.status_tag);
    if (payload.xp_tags    !== undefined) cmd += ' --xp '      + JSON.stringify(payload.xp_tags || '');
    if (payload.recur      !== undefined) cmd += ' --recur '    + JSON.stringify(payload.recur || '');
    if (payload.depends_on !== undefined) cmd += ' --depends '  + JSON.stringify(payload.depends_on || '');
    if (payload.priority   !== undefined) cmd += ' --priority ' + JSON.stringify(payload.priority || '');
    if (payload.wait_until !== undefined) cmd += ' --snooze '   + JSON.stringify(payload.wait_until || '');
    return cmd;
  }

  function buildCloseCmd(payload) {
    var meta = null;
    for (var i = 0; i < PROJECTS_META.length; i++) {
      if (PROJECTS_META[i].label === payload.project) { meta = PROJECTS_META[i]; break; }
    }
    var dir = meta ? meta.dir : '.';
    var prefix = (payload.project !== 'Master') ? 'cd ' + dir + ' && ' : '';
    return prefix + 'python3 scripts/todo.py ' + payload.mode + ' ' + payload.id;
  }

  function _doSave(mode) {
    if (!_editItem) return;
    var item = _editItem;

    var titleVal = document.getElementById('edit-title').value.trim();
    if (!titleVal) {
      editResult.textContent = 'Title is required.'; editResult.className = 'add-result err';
      return;
    }

    // Clearable fields are sent as "" (not null): JSON null arrives server-side
    // as "field absent" and is skipped, so null could never clear anything.
    var payload = {
      project:    item._project,
      id:         item.raw_id,
      base_fp:    item._fp,
      section:    editSection.value,
      title:      titleVal,
      owner:      document.getElementById('edit-owner').value.trim(),
      deadline:   document.getElementById('edit-deadline').value,
      status_tag: document.getElementById('edit-status-tag').value.trim() || null,
      xp_tags:    document.getElementById('edit-xp').value.trim(),
      recur:      document.getElementById('edit-recur').value.trim(),
      depends_on: document.getElementById('edit-depends').value.trim(),
      priority:   document.getElementById('edit-priority').value,
      wait_until: document.getElementById('edit-snooze').value,
    };

    var endpoint = '/api/update';
    if (mode === 'done' || mode === 'archive') {
      payload = { project: item._project, id: item.raw_id, base_fp: item._fp, mode: mode };
      endpoint = '/api/done';
    }

    editCmdWrap.style.display = 'none';

    if (SERVED) {
      editResult.textContent = 'Saving…'; editResult.className = 'add-result';
      fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Tracker': '1'},
        body: JSON.stringify(payload)
      })
        .then(function(r) { return r.json().then(function(j) { return {status: r.status, body: j}; }); })
        .then(function(res) {
          var j = res.body;
          if (res.status === 409) {
            editResult.textContent = '⚠️ Task changed since page loaded — ';
            editResult.className = 'add-result err';
            var rl = document.createElement('a');
            rl.href = '#'; rl.textContent = 'Refresh';
            rl.onclick = function(e) { e.preventDefault(); location.reload(); };
            editResult.appendChild(rl);
          } else if (j.ok) {
            editResult.textContent = '✅ Saved. Refreshing…';
            editResult.className = 'add-result ok';
            setTimeout(function() { location.reload(); }, 900);
          } else {
            editResult.textContent = '❌ ' + (j.error || 'Unknown error');
            editResult.className = 'add-result err';
          }
        })
        .catch(function() {
          editResult.textContent = 'Server unreachable — copy the command:';
          editResult.className = 'add-result err';
          editCmd.textContent = mode ? buildCloseCmd(payload) : buildEditCmd(payload);
          editCmdWrap.style.display = '';
        });
    } else {
      editCmd.textContent = mode ? buildCloseCmd(payload) : buildEditCmd(payload);
      editCmdWrap.style.display = '';
      editResult.textContent = '';
    }
  }

  document.getElementById('edit-save').onclick    = function() { _doSave(null); };
  document.getElementById('edit-done').onclick    = function() { _doSave('done'); };
  document.getElementById('edit-archive').onclick = function() { _doSave('archive'); };

  document.getElementById('edit-copy').onclick = function() {
    var btn = document.getElementById('edit-copy');
    navigator.clipboard.writeText(editCmd.textContent).then(function() {
      btn.textContent = 'Copied!';
      setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
    });
  };

  // ── Keyboard shortcuts ────────────────────────────────────────────────────
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      if (addOverlay.classList.contains('open')) { closeAdd(); }
      else if (editOverlay.classList.contains('open')) { closeEdit(); }
    }
  });

  // ── Boot ──────────────────────────────────────────────────────────────────
  loadState();
  applyTheme();
  if (state.search) { si.value = state.search; sc.style.display = 'block'; }
  render();
})();
</script>

</body>
</html>"""


def render_html(all_open_items, window_days, generated_date, today_iso):
    """Return a self-contained HTML dashboard with item data embedded as JSON."""
    data = []
    for item in all_open_items:
        d = dict(item)
        if "_surfaced" not in d:
            d["_surfaced"] = True   # master items are always surfaced
        # Ensure _xp_tags is a list (may already be set by fetch functions)
        if "_xp_tags" not in d:
            d["_xp_tags"] = []
        # Drop _db to keep the embedded JSON lean
        d.pop("_db", None)
        # Fingerprint used by the browser for optimistic-concurrency on edits.
        # Must include the raw xp_tags string so it matches the server-side
        # recomputation in serve.py (_check_fingerprint hashes the live column).
        d["_fp"] = item_fingerprint(d)
        d.pop("xp_tags", None)   # client uses the parsed _xp_tags list
        data.append(d)

    # Guard against </script> and <!-- sequences in embedded JSON by escaping
    # every "<" as \\u003c — valid JSON, inert in HTML. (String-level tricks
    # like "<\\!--" are NOT valid JSON escapes and would break JSON.parse.)
    data_json = json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c")

    meta_json = json.dumps(build_projects_meta(), ensure_ascii=False).replace("<", "\\u003c")

    html = DASHBOARD_TEMPLATE
    html = html.replace("__TITLE__",              PROJECT_TITLE)
    html = html.replace("__GENERATED__",          generated_date)
    html = html.replace("__WINDOW_DAYS__",        str(window_days))
    html = html.replace("__WINDOW_DAYS_INT__",    str(window_days))
    html = html.replace("__TODAY_ISO__",          today_iso)
    html = html.replace("__DATA_JSON__",          data_json)
    html = html.replace("__PROJECTS_META_JSON__", meta_json)
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="rollup.py",
        description="Aggregate cross-project priorities into MASTER_PRIORITIES.md (or dashboard.html)",
    )
    p.add_argument("--window-days", type=int, default=None,
                   help=f"Deadline window in days (config default: {ROLLUP_WINDOW_DAYS})")
    p.add_argument("--output", metavar="FILE", default=None,
                   help=f"Output path (default: MASTER_PRIORITIES.md or dashboard.html)")
    p.add_argument("--json", action="store_true",
                   help="Print JSON to stdout instead of writing the file")
    p.add_argument("--html", action="store_true",
                   help="Write self-contained HTML dashboard instead of markdown")
    return p


def _sort_key(r):
    dl = r.get("deadline") or "9999-99-99"
    return (dl, r.get("_project", ""), r.get("title", ""))


def main():
    args   = build_parser().parse_args()
    window = args.window_days if args.window_days is not None else ROLLUP_WINDOW_DAYS
    today  = date.today()
    cutoff = (today + timedelta(days=window)).isoformat()

    if args.html:
        # HTML mode: embed ALL open items per project (for drill-down beyond the window).
        # Master items surface unconditionally except when snoozed
        # (fetch_master_items sets _surfaced accordingly).
        all_open = list(fetch_master_items(cutoff))
        n_projects = 0
        for label, tail in PROJECTS:
            db_path = resolve_project_db(tail)
            if db_path is None:
                print(f"WARNING: {label}: no action_items.db for '{tail}' under PROJECT_ROOTS — skipping",
                      file=sys.stderr)
                continue
            try:
                all_open.extend(fetch_project_items_all(label, db_path, cutoff))
            except sqlite3.Error as e:
                print(f"WARNING: {label}: {db_path} unreadable ({e}) — skipping",
                      file=sys.stderr)
                continue
            n_projects += 1

        all_open.sort(key=_sort_key)

        generated = today.isoformat()
        html = render_html(all_open, window, generated, generated)

        out_path = Path(args.output) if args.output else HTML_OUT_PATH
        out_path.write_text(html, encoding="utf-8")
        print(f"Written: {out_path}  ({len(all_open)} items, {n_projects} project DBs scanned)")
        return

    # ---------------------------------------------------------------------------
    # Markdown mode (default)
    # ---------------------------------------------------------------------------
    # Snoozed master items are suppressed here, mirroring the project fetchers.
    all_items = [i for i in fetch_master_items(cutoff) if i["_surfaced"]]

    for label, tail in PROJECTS:
        db_path = resolve_project_db(tail)
        if db_path is None:
            print(f"WARNING: {label}: no action_items.db for '{tail}' under PROJECT_ROOTS — skipping",
                  file=sys.stderr)
            continue
        try:
            items = fetch_project_items(label, db_path, cutoff)
        except sqlite3.Error as e:
            print(f"WARNING: {label}: {db_path} unreadable ({e}) — skipping",
                  file=sys.stderr)
            continue
        all_items.extend(items)

    all_items.sort(key=_sort_key)

    if args.json:
        print(json.dumps(all_items, indent=2, default=str))
        return

    generated = today.isoformat()
    md = render_md(all_items, window, generated)

    out_path = Path(args.output) if args.output else MD_OUT_PATH
    out_path.write_text(md, encoding="utf-8")
    print(f"Written: {out_path}  ({len(all_items)} items, {len(PROJECTS)} project DBs scanned)")


if __name__ == "__main__":
    main()
