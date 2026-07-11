#!/usr/bin/env python3
"""
Thin CLI for action_items.db — parameterized reads/writes so escaping is never a concern.
Self-contained engine; per-project customisation lives in tracker_config.py (sibling file).

Subcommands:
  init    Create an empty action_items.db in the project root (run once at setup)
  list    [--all] [--owner] [--section S] [--status TAG] [--due-before DATE]
          [--standing] [--limit N] [--json]
  ready   [--section S] [--limit N] [--json]   (open items with no active blockers)
  show    <id>
  claim   <id> [--by NAME]   (atomically mark IN PROGRESS; fails if already claimed)
  prime   [--limit N]        (agent-ready context dump: counts, ready/overdue, conventions)
  add     --section S --title "..." [--owner ...] [--deadline ...] [--recur RULE] [--depends ID[,ID]]
          [--status "..."|--status-file FILE]
  update  <id> [--title ...] [--owner ...] [--deadline ...] [--tag ...] [--recur RULE] [--depends ID[,ID]]
          [--status "..."|--status-file -]
  append  <id> [--text "..."|--text-file -]
  done    <id>
  archive <id>
  migrate-ids   (one-time: numeric ids -> hash ids; old id kept as legacy_id)
  export  [--output FILE]   (default: action_items.md next to action_items.db)

Ids are 4-char lowercase-hex hashes with at least one letter (e.g. a3f8) —
collision-free across machines and never reused. Pre-migration numeric ids
keep working everywhere via the legacy_id fallback.
"""
import argparse
import calendar
import getpass
import json
import random
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Pin stdin/stdout/stderr to UTF-8 regardless of the system locale.
# Prevents UnicodeEncodeError when emoji or accented characters are piped
# in via --status-file - or printed in non-interactive / C-locale contexts.
# reconfigure() is Python 3.7+; the guard handles replaced/redirected streams.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# Load per-project config (tracker_config.py, sibling to this file)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

try:
    import tracker_config as _cfg
except ImportError:
    _cfg = None   # all defaults


def _c(name, default):
    """Return config attribute or default."""
    return getattr(_cfg, name, default) if _cfg else default


# ---------------------------------------------------------------------------
# Path resolution (project root = parent of scripts/)
# ---------------------------------------------------------------------------

BASE         = _HERE.parent
DB_PATH      = BASE / "action_items.db"
MD_PATH      = BASE / "action_items.md"
ARCHIVE_PATH = BASE / "action_items_archive.md"

# ---------------------------------------------------------------------------
# Config-driven constants — override any of these in tracker_config.py
# ---------------------------------------------------------------------------

PROJECT_TITLE = _c("PROJECT_TITLE", "Project")
INTRO_BLURB   = _c("INTRO_BLURB",
                    "Items are grouped by theme. Completed items moved to the bottom.")

SECTION_ORDER   = _c("SECTION_ORDER", [])
SLUG_TO_DISPLAY = dict(SECTION_ORDER)
SECTION_SLUGS   = [s for s, _ in SECTION_ORDER]
# SECTION_MAP: display header text → slug (for migration fuzzy matching).
# Config may override with legacy header aliases not in SECTION_ORDER.
SECTION_MAP = _c("SECTION_MAP", {d: s for s, d in SECTION_ORDER})

STANDING_SLUG = _c("STANDING_SLUG", "standing_watch")

# OWNER_TAG_VARIANTS: list of substring patterns that flag a "personal owner" item.
# Set to [] to disable the feature (no --owner flag, no ★ marker).
OWNER_TAG_VARIANTS = _c("OWNER_TAG_VARIANTS", [])   # e.g. ["[ME]", "[MINE]"]
OWNER_TAG_LABEL    = _c("OWNER_TAG_LABEL",    "Owner")  # used in --help text

# Status keywords recognised when auto-extracting a tag from free-text status.
# These are only a default starting set — override STATUS_KEYWORDS in
# tracker_config.py to match your own workflow. The dashboard's status filters
# are built dynamically from whatever tags actually exist in the DB, so any
# free-form `--tag` value still shows up regardless of this list. Keep "OPEN"
# (the fallback tag) plus "DONE"/"ARCHIVED" (set by the done/archive commands).
STATUS_KEYWORDS = _c("STATUS_KEYWORDS", [
    "IN PROGRESS", "BLOCKED", "ON HOLD", "QUEUED",
    "TODO", "ACTIVE", "OPEN", "DONE", "ARCHIVED", "CANCELLED",
])

# Tags treated as "closed" — hidden from active list/rollup views.
CLOSED_TAGS = _c("CLOSED_TAGS", frozenset([
    "DONE", "ARCHIVED", "CANCELLED",
]))

STANDING_PREAMBLE = _c("STANDING_PREAMBLE",
    "> Items here are durable monitors or behavioural norms — not open action items.\n"
    "> `/todo` skips this section. Add here when an item has no deliverable and no closure date.")

# String of emoji characters recognised as status markers (leading char in status_detail).
EMOJI_SET = _c("EMOJI_SET", "✅🔴🟡⚠️🟢⏳")


def _emoji_tokens(s):
    """Split a string of emoji into a list of tokens, keeping variation selectors
    (e.g. U+FE0F) attached to their base character.  ⚠️ = U+26A0 + U+FE0F is one
    token; iterating code-points would split it and lose the emoji presentation."""
    tokens = []
    for ch in s:
        if ch == '️' and tokens:  # variation selector-16: attach to previous
            tokens[-1] += ch
        else:
            tokens.append(ch)
    return tokens


# Pre-tokenized, longest-first so VS16 variants win over bare base code points.
EMOJI_TOKENS = sorted(_emoji_tokens(EMOJI_SET), key=len, reverse=True)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def extract_status_tag(text, keywords=None):
    if keywords is None:
        keywords = STATUS_KEYWORDS
    clean = re.sub(r'\*\*', '', text).strip().upper()
    # Word-boundary matches only: a bare substring test would close items whose
    # prose merely contains a tag ("PRESENTATION" ⊃ SENT, "WITHHELD" ⊃ HELD).
    for kw in keywords:
        if re.match(rf'{re.escape(kw)}\b', clean):
            return kw
    for kw in keywords:
        if re.search(rf'\b{re.escape(kw)}\b', clean[:60]):
            return kw
    return "OPEN"


def extract_deadline(text):
    m = re.search(r'(?:deadline|due\s*(?:date)?)[:\s]+(\d{4}-\d{2}-\d{2})', text, re.I)
    if m:
        return m.group(1)
    for m in re.finditer(r'\*\*([^*]*?)\*\*', text):
        span = m.group(1)
        # Skip this tracker's own status-log timestamp convention
        # ("**YYYY-MM-DD:** note text" — injected by cmd_append and used
        # manually throughout status_detail logs); it is never a deadline.
        if re.fullmatch(r'\s*20\d{2}-\d{2}-\d{2}:?\s*', span):
            continue
        dm = re.search(r'(20\d{2}-\d{2}-\d{2})', span)
        if dm:
            return dm.group(1)
    return None


_RECUR_KEYWORDS = {
    'daily': ('d', 1), 'weekly': ('w', 1), 'monthly': ('m', 1), 'yearly': ('y', 1),
}

_PRIORITY_VALUES = frozenset({'H', 'M', 'L'})


def parse_priority(s):
    """Return 'H', 'M', or 'L', or raise ValueError on invalid input."""
    v = (s or '').strip().upper()
    if v not in _PRIORITY_VALUES:
        raise ValueError(f"invalid priority {s!r}; use H, M, or L")
    return v


def _valid_iso_date(s):
    """Return the ISO date string if valid, else raise ValueError."""
    try:
        date.fromisoformat(s)
        return s
    except (ValueError, TypeError):
        raise ValueError(f"invalid date {s!r}; expected YYYY-MM-DD")


def parse_recur(rule):
    """Parse a recurrence rule into (unit, n) or raise ValueError.

    Accepted: daily/weekly/monthly/yearly  or  Nd/Nw/Nm/Ny (every N units).
    Returns ('d'|'w'|'m'|'y', int).
    """
    if not rule:
        raise ValueError("empty recurrence rule")
    r = rule.strip().lower()
    if r in _RECUR_KEYWORDS:
        return _RECUR_KEYWORDS[r]
    m = re.match(r'^(\d+)([dwmy])$', r)
    if m:
        return (m.group(2), int(m.group(1)))
    raise ValueError(
        f"unrecognised recurrence rule {rule!r}; "
        "use: daily/weekly/monthly/yearly  or  Nd/Nw/Nm/Ny"
    )


def _advance_one(dt, unit, n):
    """Advance dt by one recurrence interval."""
    if unit == 'd':
        return dt + timedelta(days=n)
    elif unit == 'w':
        return dt + timedelta(weeks=n)
    elif unit == 'm':
        month = dt.month + n
        year  = dt.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day   = min(dt.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    else:  # 'y'
        year = dt.year + n
        day  = min(dt.day, calendar.monthrange(year, dt.month)[1])
        return date(year, dt.month, day)


def next_deadline(base_iso, rule, today_iso):
    """Return the next occurrence date strictly after today_iso.

    Always advances at least one interval from base_iso so early completion
    (marking done before the deadline) produces the next occurrence rather than
    echoing the same deadline.  Month-end clamping: Jan 31 + monthly → Feb 28.
    """
    unit, n = parse_recur(rule)
    dt    = date.fromisoformat(base_iso)
    today = date.fromisoformat(today_iso)
    dt = _advance_one(dt, unit, n)   # always advance at least once
    while dt <= today:
        dt = _advance_one(dt, unit, n)
    return dt.isoformat()


def extract_emoji(text, tokens=None):
    if tokens is None:
        tokens = EMOJI_TOKENS
    clean = re.sub(r'\*\*', '', text).strip()
    # Match longest token first so ⚠️ (U+26A0 + U+FE0F) is returned whole,
    # not truncated to the bare base code point ⚠.
    for tok in tokens:
        if clean.startswith(tok):
            return tok
    return ""


def is_owner_item(title):
    """True if title contains any of the configured owner-tag patterns."""
    if not OWNER_TAG_VARIANTS:
        return False
    return any(tag in title for tag in OWNER_TAG_VARIANTS)


def slug_for_header(header):
    """Map a markdown section header string to a section slug."""
    for name, slug in SECTION_MAP.items():
        if name.lower() in header.lower() or header.lower() in name.lower():
            return slug, slug == STANDING_SLUG
    return re.sub(r'[^a-z0-9]+', '_', header.lower()).strip('_'), False


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    raw_id        TEXT PRIMARY KEY,
    sort_id       INTEGER,
    section       TEXT NOT NULL,
    title         TEXT NOT NULL,
    owner         TEXT,
    source_date   TEXT,
    deadline      TEXT,
    status_tag    TEXT,
    status_emoji  TEXT,
    is_owner      INTEGER DEFAULT 0,
    is_standing   INTEGER DEFAULT 0,
    status_detail TEXT,
    xp_tags       TEXT,
    recur         TEXT,
    depends_on    TEXT,
    priority      TEXT,
    wait_until    TEXT
);
CREATE INDEX IF NOT EXISTS idx_status   ON items(status_tag);
CREATE INDEX IF NOT EXISTS idx_section  ON items(section);
CREATE INDEX IF NOT EXISTS idx_owner    ON items(is_owner);
CREATE INDEX IF NOT EXISTS idx_deadline ON items(deadline);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER
);
"""

# Columns added after the initial schema.  open_db() applies these automatically
# to pre-existing DBs so users don't need to migrate manually.
_MIGRATIONS = [
    ("xp_tags",    "TEXT"),
    ("recur",      "TEXT"),
    ("depends_on", "TEXT"),
    ("priority",   "TEXT"),
    ("wait_until", "TEXT"),
    ("legacy_id",  "TEXT"),
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db():
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}\n  Run: python3 scripts/todo.py init")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Wait up to 5 s if another writer (server, concurrent CLI) holds the lock.
    conn.execute("PRAGMA busy_timeout=5000")
    # Apply any column additions that postdate the original CREATE TABLE so
    # existing DBs upgrade in place without manual migration steps.
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(items)")
    existing = {row[1] for row in cur.fetchall()}
    for col, col_type in _MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {col_type}")
    # meta table postdates the original schema (holds the monotone id counter).
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)")
    # Registry of every hash id ever issued — closing an item deletes its row,
    # so this is what preserves the never-reuse invariant for hash ids.
    conn.execute("CREATE TABLE IF NOT EXISTS issued_ids (id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def _next_id(cur):
    """Allocate the next item id monotonically — never reuses the id of a
    closed (deleted) item, so archive rows and depends_on references stay
    unambiguous.  Must be called inside an open BEGIN IMMEDIATE transaction.

    Seeds from MAX(sort_id) on DBs that predate the meta counter."""
    cur.execute("SELECT value FROM meta WHERE key = 'next_sort_id'")
    row = cur.fetchone()
    cur.execute("SELECT MAX(sort_id) FROM items")
    max_sort = cur.fetchone()[0] or 0
    nxt = max(row[0] if row else 0, max_sort + 1)
    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('next_sort_id', ?)",
                (nxt + 1,))
    return nxt


_HASH_CHARS = "0123456789abcdef"


def _new_hash_id(cur, length=4):
    """Allocate a new collision-free hash id.  Must be called inside an open
    BEGIN IMMEDIATE transaction.

    Lowercase hex with at least one letter, so a hash id can never collide
    with a legacy numeric id (in the DB, the archive, or prose references).
    Registered in issued_ids so a closed item's id is never reissued."""
    tries = 0
    while True:
        h = ''.join(random.choice(_HASH_CHARS) for _ in range(length))
        tries += 1
        if tries > 100:          # id space getting crowded — widen it
            length += 1
            tries = 0
        if not any(c.isalpha() for c in h):
            continue
        cur.execute("SELECT 1 FROM issued_ids WHERE id = ?", (h,))
        if cur.fetchone():
            continue
        cur.execute("SELECT 1 FROM items WHERE raw_id = ? OR legacy_id = ?", (h, h))
        if cur.fetchone():
            continue
        cur.execute("INSERT INTO issued_ids (id) VALUES (?)", (h,))
        return h


def _resolve_item(cur, item_id):
    """Fetch an item by raw_id, falling back to legacy_id (pre-hash numeric id).
    Returns the row or None."""
    cur.execute("SELECT * FROM items WHERE raw_id = ?", (item_id,))
    row = cur.fetchone()
    if row:
        return row
    cur.execute("SELECT * FROM items WHERE legacy_id = ?", (item_id,))
    rows = cur.fetchall()
    return rows[0] if len(rows) == 1 else None


def _normalize_depends(cur, depends_str):
    """Rewrite legacy numeric ids in a --depends list to their canonical raw_id.
    Unknown ids pass through unchanged (dependencies are informational)."""
    if not depends_str:
        return depends_str
    out = []
    for tok in (x.strip() for x in depends_str.split(',')):
        if not tok:
            continue
        row = _resolve_item(cur, tok)
        out.append(row['raw_id'] if row else tok)
    return ",".join(out)


def read_status_arg(args):
    if hasattr(args, 'status_file') and args.status_file:
        if args.status_file == '-':
            return sys.stdin.read().rstrip('\n')
        return Path(args.status_file).read_text(encoding='utf-8').rstrip('\n')
    if hasattr(args, 'status') and args.status is not None:
        return args.status
    return None


def read_text_arg(args):
    if hasattr(args, 'text_file') and args.text_file:
        if args.text_file == '-':
            return sys.stdin.read().rstrip('\n')
        return Path(args.text_file).read_text(encoding='utf-8').rstrip('\n')
    if hasattr(args, 'text') and args.text is not None:
        return args.text
    return None


def derive_fields(status_detail, title=None, override_tag=None, override_deadline=None):
    tag      = override_tag      or extract_status_tag(status_detail or "")
    deadline = override_deadline or extract_deadline(status_detail or "")
    emoji    = extract_emoji(status_detail or "")
    owner    = is_owner_item(title) if title is not None else None
    return tag, deadline, emoji, owner


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args):
    if DB_PATH.exists():
        print(f"DB already exists: {DB_PATH}")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    print(f"Initialised: {DB_PATH}")
    # Seed an empty markdown view
    _export(MD_PATH)
    print(f"Created empty: {MD_PATH}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(args):
    conn = open_db()
    cur  = conn.cursor()

    conditions = []
    params     = []

    today_iso = date.today().isoformat()

    if getattr(args, 'snoozed', False):
        # Show only items currently hidden by a snooze date
        conditions.append("wait_until IS NOT NULL AND wait_until > ?")
        params.append(today_iso)
    elif not args.all and not args.standing:
        placeholders = ",".join("?" for _ in CLOSED_TAGS)
        conditions.append(f"status_tag NOT IN ({placeholders})")
        params.extend(CLOSED_TAGS)
        conditions.append("is_standing = 0")
        # Hide snoozed items (wait_until in the future)
        conditions.append("(wait_until IS NULL OR wait_until <= ?)")
        params.append(today_iso)
    elif args.standing:
        conditions.append("is_standing = 1")

    if args.owner and OWNER_TAG_VARIANTS:
        conditions.append("is_owner = 1")
    if args.section:
        conditions.append("section = ?")
        params.append(args.section)
    if args.status:
        conditions.append("status_tag = ?")
        params.append(args.status.upper())
    if args.due_before:
        conditions.append("deadline IS NOT NULL AND deadline <= ?")
        params.append(args.due_before)
    if getattr(args, 'search', None):
        # Escape LIKE wildcards so a literal % or _ in the search term matches itself.
        escaped = re.sub(r'([\\%_])', r'\\\1', args.search)
        term = f"%{escaped}%"
        conditions.append("(title LIKE ? ESCAPE '\\' OR status_detail LIKE ? ESCAPE '\\'"
                          " OR status_tag LIKE ? ESCAPE '\\')")
        params += [term, term, term]

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT raw_id, legacy_id, title, owner, deadline, status_emoji, status_tag, is_owner,
               section, depends_on, recur, priority, wait_until
        FROM items
        {where}
        ORDER BY
            CASE WHEN deadline IS NULL THEN '9999' ELSE deadline END,
            CASE priority WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END,
            is_owner DESC,
            sort_id
        LIMIT ?
    """
    params.append(args.limit)
    cur.execute(sql, params)
    rows = cur.fetchall()

    # Compute blocked_by: a dependency raw_id is still active if its row exists.
    # (Closed items are deleted, so presence in items == active.)
    cur.execute("SELECT raw_id FROM items")
    active_ids = {row[0] for row in cur.fetchall()}
    conn.close()

    if args.json:
        print(json.dumps(_rows_json(rows, active_ids), indent=2))
        return

    if not rows:
        print("(no items)")
        return

    _print_rows(rows, active_ids)
    print(f"\n({len(rows)} items shown)")


def _blocked_by(row, active_ids):
    """Active (still-open) prerequisite ids for a row."""
    dep_ids = [x.strip() for x in (row['depends_on'] or '').split(',') if x.strip()]
    return [x for x in dep_ids if x in active_ids]


def _rows_json(rows, active_ids):
    out = []
    for r in rows:
        d = dict(r)
        d['blocked_by'] = _blocked_by(r, active_ids)
        out.append(d)
    return out


def _print_rows(rows, active_ids):
    header = f"{'#':<6} {'Title':<55} {'Owner':<20} {'Deadline':<11} {'Status'}"
    print(header)
    print("-" * len(header))
    for r in rows:
        title_short  = re.sub(r'\*\*', '', r['title'])[:55]
        owner_short  = (r['owner'] or '')[:20]
        deadline     = r['deadline'] or '—'
        tag          = (r['status_emoji'] or '') + (r['status_tag'] or '')
        marker       = "★" if (r['is_owner'] and OWNER_TAG_VARIANTS) else " "
        pri_str      = f" [{r['priority']}]" if r['priority'] else ""
        recur_str    = f"  🔁 {r['recur']}" if r['recur'] else ""
        snooze_str   = f"  💤 until {r['wait_until']}" if r['wait_until'] else ""
        blocked_by   = _blocked_by(r, active_ids)
        blocked_str  = ("  🔒 blocked by: " + ", ".join(f"#{x}" for x in blocked_by)) if blocked_by else ""
        print(f"{marker}{r['raw_id']:<5} {title_short:<55} {owner_short:<20} {deadline:<11} {tag}{pri_str}{recur_str}{snooze_str}{blocked_str}")


# ---------------------------------------------------------------------------
# ready
# ---------------------------------------------------------------------------

_READY_SELECT = """
    SELECT raw_id, legacy_id, title, owner, deadline, status_emoji, status_tag, is_owner,
           section, depends_on, recur, priority, wait_until
    FROM items
    {where}
    ORDER BY
        CASE WHEN deadline IS NULL THEN '9999' ELSE deadline END,
        CASE priority WHEN 'H' THEN 0 WHEN 'M' THEN 1 WHEN 'L' THEN 2 ELSE 3 END,
        is_owner DESC,
        sort_id
"""


def _fetch_ready(cur, today_iso, section=None):
    """Open, non-standing, non-snoozed rows with no active blockers, plus active_ids."""
    placeholders = ",".join("?" for _ in CLOSED_TAGS)
    conditions = [f"status_tag NOT IN ({placeholders})",
                  "is_standing = 0",
                  "(wait_until IS NULL OR wait_until <= ?)"]
    params = list(CLOSED_TAGS) + [today_iso]
    if section:
        conditions.append("section = ?")
        params.append(section)
    cur.execute(_READY_SELECT.format(where="WHERE " + " AND ".join(conditions)), params)
    rows = cur.fetchall()
    cur.execute("SELECT raw_id FROM items")
    active_ids = {row[0] for row in cur.fetchall()}
    return [r for r in rows if not _blocked_by(r, active_ids)], active_ids


def cmd_ready(args):
    conn = open_db()
    cur  = conn.cursor()
    ready, active_ids = _fetch_ready(cur, date.today().isoformat(), args.section)
    conn.close()
    ready = ready[:args.limit]

    if args.json:
        print(json.dumps(_rows_json(ready, active_ids), indent=2))
        return

    if not ready:
        print("(no ready items)")
        return

    _print_rows(ready, active_ids)
    print(f"\n({len(ready)} ready items — open, unblocked, not snoozed)")


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

def cmd_claim(args):
    actor = args.by or getpass.getuser()
    conn = open_db()
    cur  = conn.cursor()
    # Write lock before the read so two concurrent claims serialize: the loser
    # re-reads the row after the winner commits and sees IN PROGRESS.
    conn.execute("BEGIN IMMEDIATE")
    row = _resolve_item(cur, args.id)
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")
    rid = row['raw_id']
    if row['status_tag'] == 'IN PROGRESS':
        holder = row['owner'] or 'unspecified'
        conn.close()
        sys.exit(f"Item #{rid} is already claimed / IN PROGRESS (owner: {holder}). "
                 f"Release with: update {rid} --tag OPEN")

    cur.execute("SELECT raw_id FROM items")
    active_ids = {r[0] for r in cur.fetchall()}
    blocked = _blocked_by(row, active_ids)

    today      = date.today().isoformat()
    new_detail = (row['status_detail'] or '').rstrip() + f"\n**{today}:** claimed by {actor}"
    new_owner  = row['owner'] or actor
    cur.execute("UPDATE items SET status_tag = 'IN PROGRESS', owner = ?, status_detail = ? "
                "WHERE raw_id = ?", (new_owner, new_detail, rid))
    conn.commit()
    conn.close()
    print(f"Claimed item #{rid} for {actor} (status IN PROGRESS)")
    if blocked:
        print("WARNING: item is blocked by: " + ", ".join(f"#{x}" for x in blocked),
              file=sys.stderr)


# ---------------------------------------------------------------------------
# prime
# ---------------------------------------------------------------------------

PRIME_CONVENTIONS = """## Conventions (agent contract)
- All writes go through scripts/todo.py — never edit action_items.md (auto-generated, overwritten).
- Prefer `append <id> --text "..."` for status notes: non-destructive, auto-dated.
- `done <id>` permanently deletes the row and archives it; recurring items respawn. `archive <id>` retires without respawn.
- `claim <id> [--by NAME]` atomically marks IN PROGRESS; fails if already claimed. Release: `update <id> --tag OPEN`.
- Prefer `list --json` / `ready --json` over parsing markdown."""


def cmd_prime(args):
    conn = open_db()
    cur  = conn.cursor()
    today_iso = date.today().isoformat()
    week_iso  = (date.today() + timedelta(days=7)).isoformat()
    placeholders = ",".join("?" for _ in CLOSED_TAGS)

    def _count(where, params=()):
        cur.execute(f"SELECT COUNT(*) FROM items WHERE {where}", params)
        return cur.fetchone()[0]

    open_where = f"status_tag NOT IN ({placeholders}) AND is_standing = 0"
    # Overdue / due-soon counts exclude snoozed items so they match the tables below.
    unsnoozed  = " AND (wait_until IS NULL OR wait_until <= ?)"
    n_open     = _count(open_where, tuple(CLOSED_TAGS))
    # deadline != '' guards against empty-string deadlines in DBs written by old engines
    n_overdue  = _count(open_where + " AND deadline IS NOT NULL AND deadline != '' AND deadline < ?" + unsnoozed,
                        (*CLOSED_TAGS, today_iso, today_iso))
    n_due7     = _count(open_where + " AND deadline IS NOT NULL AND deadline != '' AND deadline >= ? AND deadline <= ?" + unsnoozed,
                        (*CLOSED_TAGS, today_iso, week_iso, today_iso))
    n_snoozed  = _count("wait_until IS NOT NULL AND wait_until > ?", (today_iso,))
    n_standing = _count("is_standing = 1")

    ready, active_ids = _fetch_ready(cur, today_iso)
    cur.execute(_READY_SELECT.format(where=f"WHERE {open_where}"), tuple(CLOSED_TAGS))
    open_rows = cur.fetchall()
    blocked_rows = [r for r in open_rows if _blocked_by(r, active_ids)]
    n_blocked = len(blocked_rows)
    overdue_rows = [r for r in open_rows
                    if r['deadline'] and r['deadline'] < today_iso
                    and not (r['wait_until'] and r['wait_until'] > today_iso)]
    conn.close()

    print(f"# {PROJECT_TITLE} tracker — context ({today_iso})")
    print(f"Engine: scripts/todo.py | DB: {DB_PATH.name}")
    if SECTION_ORDER:
        print("Sections: " + ", ".join(f"{slug} ({disp})" for slug, disp in SECTION_ORDER))
    print(f"Counts: {n_open} open | {n_overdue} overdue | {n_due7} due ≤7d | "
          f"{len(ready)} ready | {n_blocked} blocked | {n_snoozed} snoozed | {n_standing} standing")

    if overdue_rows:
        print(f"\n## Overdue ({len(overdue_rows)})")
        _print_rows(overdue_rows[:args.limit], active_ids)
    if ready:
        print(f"\n## Ready now — open, unblocked, not snoozed (top {min(len(ready), args.limit)})")
        _print_rows(ready[:args.limit], active_ids)
    if blocked_rows:
        print(f"\n## Blocked ({n_blocked})")
        _print_rows(blocked_rows[:args.limit], active_ids)

    print()
    print(PRIME_CONVENTIONS)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def cmd_show(args):
    conn = open_db()
    cur  = conn.cursor()
    row = _resolve_item(cur, args.id)
    conn.close()
    if not row:
        sys.exit(f"No item with id '{args.id}'")
    for key in row.keys():
        val = row[key]
        if val is None:
            continue
        if key == 'status_detail':
            print(f"\n--- status_detail ---\n{val}\n---")
        else:
            print(f"{key:<14}: {val}")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def cmd_add(args):
    if args.section is None:
        if not SECTION_SLUGS:
            sys.exit("No SECTION_ORDER configured — set it in scripts/tracker_config.py")
        args.section = SECTION_SLUGS[0]
    if args.section not in SLUG_TO_DISPLAY:
        valid = ", ".join(SECTION_SLUGS)
        sys.exit(f"Unknown section '{args.section}'. Valid: {valid}")

    if args.deadline is not None:
        args.deadline = args.deadline.strip() or None
        if args.deadline:
            try:
                _valid_iso_date(args.deadline)
            except ValueError as e:
                sys.exit(str(e))

    status_text = read_status_arg(args) or "OPEN"
    tag, deadline, emoji, _ = derive_fields(status_text, args.title, args.tag, args.deadline)
    owner_flag  = 1 if is_owner_item(args.title) else 0
    is_standing = 1 if args.section == STANDING_SLUG else 0
    today       = date.today().isoformat()

    recur_val   = getattr(args, 'recur',   None) or None
    depends_val = getattr(args, 'depends', None) or None

    if recur_val:
        try:
            parse_recur(recur_val)
        except ValueError as e:
            sys.exit(str(e))
        if not deadline:
            sys.exit("--recur requires a deadline (use --deadline YYYY-MM-DD)")

    priority_val = None
    raw_priority = getattr(args, 'priority', None)
    if raw_priority:
        try:
            priority_val = parse_priority(raw_priority)
        except ValueError as e:
            sys.exit(str(e))

    wait_val = None
    raw_wait = getattr(args, 'snooze', None)
    if raw_wait:
        try:
            wait_val = _valid_iso_date(raw_wait)
        except ValueError as e:
            sys.exit(str(e))

    conn = open_db()
    cur  = conn.cursor()
    # Acquire write lock before computing new ID so no concurrent CLI add
    # can claim the same sort_id between the counter read and INSERT.
    conn.execute("BEGIN IMMEDIATE")
    new_sort = _next_id(cur)
    new_id   = _new_hash_id(cur)
    if depends_val:
        depends_val = _normalize_depends(cur, depends_val)

    xp_tags = getattr(args, 'xp', None) or None

    cur.execute("""
        INSERT INTO items
        (raw_id, sort_id, section, title, owner, source_date, deadline,
         status_tag, status_emoji, is_owner, is_standing, status_detail,
         xp_tags, recur, depends_on, priority, wait_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_id, new_sort, args.section, args.title,
          args.owner or "", today, deadline,
          tag, emoji, owner_flag, is_standing, status_text,
          xp_tags, recur_val, depends_val, priority_val, wait_val))
    conn.commit()
    conn.close()
    print(f"Added item #{new_id} in section '{args.section}'")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def cmd_update(args):
    conn = open_db()
    cur  = conn.cursor()
    row = _resolve_item(cur, args.id)
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")
    rid = row['raw_id']

    if args.deadline is not None:
        args.deadline = args.deadline.strip()
        if args.deadline:
            try:
                _valid_iso_date(args.deadline)
            except ValueError as e:
                conn.close()
                sys.exit(str(e))

    updates = {}

    new_status = read_status_arg(args)
    if new_status is not None:
        updates['status_detail'] = new_status
        tag, deadline, emoji, _ = derive_fields(new_status, row['title'], args.tag, args.deadline)
        updates['status_tag']   = tag
        updates['deadline']     = deadline if deadline else row['deadline']
        updates['status_emoji'] = emoji

    if args.title is not None:
        updates['title']    = args.title
        updates['is_owner'] = 1 if is_owner_item(args.title) else 0
    if args.owner is not None:
        updates['owner'] = args.owner
    if args.deadline is not None:
        updates['deadline'] = args.deadline or None   # '' clears to NULL
    if args.tag is not None:
        updates['status_tag'] = args.tag.upper()
        if updates['status_tag'] in CLOSED_TAGS:
            print(f"WARNING: {updates['status_tag']!r} is a closed tag — the item stays in the "
                  f"DB (hidden from list/rollup but still exported to markdown). "
                  f"Use 'done' or 'archive' to close and archive it properly.",
                  file=sys.stderr)
    if args.section is not None:
        if args.section not in SLUG_TO_DISPLAY:
            conn.close()
            sys.exit(f"Unknown section '{args.section}'")
        updates['section']     = args.section
        updates['is_standing'] = 1 if args.section == STANDING_SLUG else 0

    xp_val = getattr(args, 'xp', None)
    if xp_val is not None:
        updates['xp_tags'] = xp_val if xp_val else None

    recur_val = getattr(args, 'recur', None)
    if recur_val is not None:
        if recur_val:
            try:
                parse_recur(recur_val)
            except ValueError as e:
                conn.close()
                sys.exit(str(e))
            new_deadline = updates['deadline'] if 'deadline' in updates else row['deadline']
            if not new_deadline:
                conn.close()
                sys.exit("--recur requires a deadline (use --deadline to set one)")
        updates['recur'] = recur_val or None

    depends_val = getattr(args, 'depends', None)
    if depends_val is not None:
        updates['depends_on'] = _normalize_depends(cur, depends_val) or None

    priority_val = getattr(args, 'priority', None)
    if priority_val is not None:
        if priority_val:
            try:
                priority_val = parse_priority(priority_val)
            except ValueError as e:
                conn.close()
                sys.exit(str(e))
        updates['priority'] = priority_val or None

    wait_val = getattr(args, 'snooze', None)
    if wait_val is not None:
        if wait_val:
            try:
                wait_val = _valid_iso_date(wait_val)
            except ValueError as e:
                conn.close()
                sys.exit(str(e))
        updates['wait_until'] = wait_val or None

    if not updates:
        conn.close()
        print("Nothing to update.")
        return

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [rid]
    cur.execute(f"UPDATE items SET {set_clause} WHERE raw_id = ?", vals)
    conn.commit()
    conn.close()
    print(f"Updated item #{rid} ({', '.join(updates.keys())})")


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------

def cmd_append(args):
    text = read_text_arg(args)
    if not text:
        sys.exit("Provide --text or --text-file")

    conn = open_db()
    cur  = conn.cursor()
    row = _resolve_item(cur, args.id)
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")
    rid = row['raw_id']

    today      = date.today().isoformat()
    new_detail = (row['status_detail'] or '').rstrip() + f"\n**{today}:** {text}"
    tag, _, emoji, _ = derive_fields(new_detail, row['title'])
    # Re-derive deadline from the newly appended text only (not full new_detail), so the
    # bold timestamp prefix **YYYY-MM-DD:** that cmd_append injects doesn't masquerade as a
    # deadline. If the appended text contains an explicit "deadline: …" keyword it will still
    # update the stored deadline; otherwise preserve whatever update --deadline set.
    derived_deadline = extract_deadline(text)
    deadline = derived_deadline if derived_deadline is not None else row['deadline']

    cur.execute("""
        UPDATE items
        SET status_detail=?, status_tag=?, deadline=?, status_emoji=?
        WHERE raw_id=?
    """, (new_detail, tag, deadline, emoji, rid))
    conn.commit()
    conn.close()
    print(f"Appended to item #{rid}")


# ---------------------------------------------------------------------------
# done / archive
# ---------------------------------------------------------------------------

def cmd_done_archive(args, final_tag):
    conn = open_db()
    cur  = conn.cursor()
    row = _resolve_item(cur, args.id)
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")
    rid = row['raw_id']

    today       = date.today().isoformat()
    status_text = row['status_detail'] or ''
    # Strip bold markers before the prefix check so "**DONE …" isn't double-prefixed.
    if not re.sub(r'\*\*', '', status_text).strip().upper().startswith(final_tag):
        status_text = f"**{final_tag} {today}** — {status_text}"

    # Capture fields needed for possible respawn before the row is deleted.
    row_recur      = row['recur']     if 'recur'      in row.keys() else None
    row_deadline   = row['deadline']
    row_depends_on = row['depends_on'] if 'depends_on' in row.keys() else None
    row_xp_tags    = row['xp_tags']   if 'xp_tags'    in row.keys() else None
    row_priority   = row['priority']  if 'priority'   in row.keys() else None

    archive_row = (
        f"| {row['raw_id']} | {row['title']} | {row['owner'] or ''} "
        f"| {row['source_date'] or ''} | {status_text} |"
    )

    archive_text = (ARCHIVE_PATH.read_text(encoding='utf-8')
                    if ARCHIVE_PATH.exists() else "# Archived Items\n\n---\n")
    if "## Archived via CLI" not in archive_text:
        archive_text  = archive_text.rstrip('\n') + "\n\n## Archived via CLI\n\n"
        archive_text += "| # | Action | Owner(s) | Source date | Status |\n"
        archive_text += "|---|--------|----------|-------------|--------|\n"
    # Idempotent append: a retry after a failed DB delete must not duplicate the row.
    if archive_row not in archive_text:
        archive_text += archive_row + "\n"
        ARCHIVE_PATH.write_text(archive_text, encoding='utf-8')

    cur.execute("DELETE FROM items WHERE raw_id = ?", (rid,))
    conn.commit()
    conn.close()
    print(f"Item #{rid} archived to {ARCHIVE_PATH.name}")

    _export(MD_PATH)
    print(f"Regenerated {MD_PATH.name}")

    # Respawn only on DONE (not archive/cancel); requires a recur rule and a deadline.
    if final_tag == "DONE" and row_recur and row_deadline:
        new_deadline = next_deadline(row_deadline, row_recur, today)
        conn2 = open_db()
        cur2  = conn2.cursor()
        conn2.execute("BEGIN IMMEDIATE")
        new_sort = _next_id(cur2)
        new_id   = _new_hash_id(cur2)
        cur2.execute("""
            INSERT INTO items
            (raw_id, sort_id, section, title, owner, source_date, deadline,
             status_tag, status_emoji, is_owner, is_standing, status_detail,
             xp_tags, recur, depends_on, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (new_id, new_sort,
              row['section'], row['title'], row['owner'] or '',
              today, new_deadline,
              'OPEN', '', row['is_owner'], row['is_standing'], 'OPEN',
              row_xp_tags, row_recur, row_depends_on, row_priority))
        conn2.commit()
        conn2.close()
        print(f"Recurring: next occurrence is #{new_id} (deadline {new_deadline})")


# ---------------------------------------------------------------------------
# migrate-ids
# ---------------------------------------------------------------------------

def cmd_migrate_ids(args):
    """One-time conversion of legacy numeric raw_ids to hash ids.

    The old id is preserved in legacy_id, so lookups, archive rows, and prose
    references to the numeric id keep resolving.  depends_on lists across the
    whole DB are rewritten to the new ids.  A timestamped backup of the DB is
    written first."""
    stamp  = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"{DB_PATH.name}.bak-{stamp}")
    shutil.copy2(DB_PATH, backup)

    conn = open_db()
    cur  = conn.cursor()
    conn.execute("BEGIN IMMEDIATE")
    cur.execute("SELECT raw_id, depends_on FROM items ORDER BY sort_id")
    rows = cur.fetchall()

    numeric = [r for r in rows if re.fullmatch(r'\d+', r['raw_id'])]
    if not numeric:
        conn.rollback()
        conn.close()
        backup.unlink()
        print("Nothing to migrate — no numeric ids found.")
        return

    mapping = {}
    for r in numeric:
        mapping[r['raw_id']] = _new_hash_id(cur)
    for old, new in mapping.items():
        cur.execute("UPDATE items SET raw_id = ?, legacy_id = ? WHERE raw_id = ?",
                    (new, old, old))
    # Rewrite depends_on lists everywhere (blocked-by matching is on raw_id).
    for r in rows:
        deps = r['depends_on']
        if not deps:
            continue
        toks    = [x.strip() for x in deps.split(',') if x.strip()]
        new_deps = ",".join(mapping.get(t, t) for t in toks)
        if new_deps != deps:
            owner_id = mapping.get(r['raw_id'], r['raw_id'])
            cur.execute("UPDATE items SET depends_on = ? WHERE raw_id = ?",
                        (new_deps, owner_id))
    conn.commit()
    conn.close()

    print(f"Backup: {backup.name}")
    print(f"Migrated {len(mapping)} items to hash ids (old id kept as legacy_id):")
    for old, new in mapping.items():
        print(f"  #{old} -> #{new}")
    _export(MD_PATH)
    print(f"Regenerated {MD_PATH.name}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

EXPORT_BANNER = (
    "<!-- AUTO-GENERATED from action_items.db via scripts/todo.py"
    " — DO NOT EDIT DIRECTLY; use: python3 scripts/todo.py update <id> --status-file - -->"
)

TABLE_HEADER = "| # | Action | Owner(s) | Source date | Status |"
TABLE_SEP    = "|---|--------|----------|-------------|--------|"


def _export(output_path):
    conn = open_db()
    cur  = conn.cursor()

    # A partially-synced DB (any cloud sync — Dropbox, Box, iCloud, Drive, NFS)
    # can leave index pages inconsistent with the table b-trees. quick_check is
    # cheap on a healthy DB; only rebuild indexes if it reports a problem.
    if cur.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        cur.execute("REINDEX")
        conn.commit()

    today = date.today().isoformat()
    lines = [
        EXPORT_BANNER,
        "",
        f"# {PROJECT_TITLE} — Open Action Items",
        "",
        INTRO_BLURB,
        "",
        f"Last updated: {today}",
        "",
        "---",
        "",
    ]

    for slug, display in SECTION_ORDER:
        cur.execute(
            "SELECT * FROM items WHERE section = ? ORDER BY sort_id",
            (slug,),
        )
        rows = cur.fetchall()
        if not rows:
            continue

        lines.append(f"## {display}")
        lines.append("")

        if slug == STANDING_SLUG:
            lines.append(STANDING_PREAMBLE)
            lines.append("")

        lines.append(TABLE_HEADER)
        lines.append(TABLE_SEP)
        for r in rows:
            title  = r['title']         or ''
            owner  = r['owner']         or ''
            src    = r['source_date']   or ''
            status = r['status_detail'] or ''
            lines.append(f"| {r['raw_id']} | {title} | {owner} | {src} | {status} |")

        lines.append("")
        lines.append("")

    conn.close()

    lines.append("---")
    lines.append("")
    lines.append("Completed items → `action_items_archive.md`")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding='utf-8')


def cmd_export(args):
    output = Path(args.output) if args.output else MD_PATH
    _export(output)
    print(f"Exported {output}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="todo.py",
        description="Thin CLI for action_items.db. Run from project root or anywhere.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    sub.add_parser("init", help="Create empty action_items.db in project root")

    # list
    pl = sub.add_parser("list", help="List action items (default: open, non-standing)")
    pl.add_argument("--all",        action="store_true", help="Include closed items")
    pl.add_argument("--owner",      action="store_true",
                    help=f"Only [{OWNER_TAG_LABEL}]-tagged items (if owner-tag configured)")
    pl.add_argument("--section",    metavar="SLUG",      help="Filter by section slug")
    pl.add_argument("--status",     metavar="TAG",       help="Filter by status tag")
    pl.add_argument("--due-before", metavar="DATE",      help="Only items with deadline <= DATE (YYYY-MM-DD)")
    pl.add_argument("--standing",   action="store_true", help="Only standing/watch items")
    pl.add_argument("--snoozed",    action="store_true", help="Only items currently snoozed (wait_until in the future)")
    pl.add_argument("--search",     metavar="TERM",      help="Filter by text in title, status, or notes")
    pl.add_argument("--limit",      type=int, default=500, metavar="N")
    pl.add_argument("--json",       action="store_true", help="JSON output")

    # ready
    pr = sub.add_parser("ready", help="List open items with no active blockers (not snoozed, not standing)")
    pr.add_argument("--section", metavar="SLUG", help="Filter by section slug")
    pr.add_argument("--limit",   type=int, default=500, metavar="N")
    pr.add_argument("--json",    action="store_true", help="JSON output")

    # show
    ps = sub.add_parser("show", help="Show full record for one item")
    ps.add_argument("id", metavar="ID")

    # claim
    pc = sub.add_parser("claim", help="Atomically mark an item IN PROGRESS (fails if already claimed)")
    pc.add_argument("id",   metavar="ID")
    pc.add_argument("--by", metavar="NAME", default=None,
                    help="Claiming actor (default: current username)")

    # prime
    pp = sub.add_parser("prime", help="Emit agent-ready context: counts, overdue/ready/blocked items, conventions")
    pp.add_argument("--limit", type=int, default=15, metavar="N",
                    help="Max rows per section (default 15)")

    # add
    pa = sub.add_parser("add", help="Add a new item")
    pa.add_argument("--section",     default=None, metavar="SLUG",
                    help="Section slug (default: first in SECTION_ORDER)")
    pa.add_argument("--title",       required=True)
    pa.add_argument("--owner",       default="")
    pa.add_argument("--deadline",    metavar="YYYY-MM-DD", default=None)
    pa.add_argument("--tag",         metavar="TAG", default=None)
    pa.add_argument("--xp",          metavar="PROJ[,PROJ]", default=None,
                    help="Comma-separated project labels this item is cross-tagged into (e.g. ProjectA,ProjectB)")
    pa.add_argument("--recur",       metavar="RULE", default=None,
                    help="Recurrence rule: daily/weekly/monthly/yearly or Nd/Nw/Nm/Ny (requires --deadline)")
    pa.add_argument("--depends",     metavar="ID[,ID]", default=None,
                    help="Comma-separated IDs of prerequisite items (informational; shown as 🔒 until met)")
    pa.add_argument("--priority",    metavar="{H,M,L}", default=None,
                    help="Priority: H (high), M (medium), or L (low)")
    pa.add_argument("--snooze",      metavar="YYYY-MM-DD", default=None,
                    help="Hide from default list until this date (--wait is an alias)")
    pa.add_argument("--wait",        dest="snooze", metavar="YYYY-MM-DD", default=None,
                    help="Alias for --snooze")
    pa.add_argument("--status",      metavar="TEXT", default=None)
    pa.add_argument("--status-file", metavar="FILE", default=None,
                    help="Read status text from file ('-' = stdin)")

    # update
    pu = sub.add_parser("update", help="Update fields of an item")
    pu.add_argument("id",            metavar="ID")
    pu.add_argument("--title",       default=None)
    pu.add_argument("--owner",       default=None)
    pu.add_argument("--deadline",    metavar="YYYY-MM-DD", default=None)
    pu.add_argument("--tag",         metavar="TAG", default=None)
    pu.add_argument("--xp",          metavar="PROJ[,PROJ]", default=None,
                    help="Set cross-project tags (comma-separated, empty string to clear)")
    pu.add_argument("--recur",       metavar="RULE", default=None,
                    help="Set recurrence rule (empty string to clear)")
    pu.add_argument("--depends",     metavar="ID[,ID]", default=None,
                    help="Set prerequisite IDs (comma-separated, empty string to clear)")
    pu.add_argument("--priority",    metavar="{H,M,L}", default=None,
                    help="Set priority (empty string to clear)")
    pu.add_argument("--snooze",      metavar="YYYY-MM-DD", default=None,
                    help="Snooze until date (empty string to clear)")
    pu.add_argument("--wait",        dest="snooze", metavar="YYYY-MM-DD", default=None,
                    help="Alias for --snooze")
    pu.add_argument("--section",     metavar="SLUG", default=None)
    pu.add_argument("--status",      metavar="TEXT", default=None)
    pu.add_argument("--status-file", metavar="FILE", default=None,
                    help="Read status text from file ('-' = stdin)")

    # append
    pap = sub.add_parser("append", help="Append a dated update to status_detail")
    pap.add_argument("id",          metavar="ID")
    pap.add_argument("--text",      default=None)
    pap.add_argument("--text-file", metavar="FILE", default=None,
                     help="Read text from file ('-' = stdin)")

    # done
    pd = sub.add_parser("done", help="Mark item DONE, archive it, regenerate MD")
    pd.add_argument("id", metavar="ID")

    # archive
    par = sub.add_parser("archive", help="Mark item ARCHIVED, archive it, regenerate MD")
    par.add_argument("id", metavar="ID")

    # migrate-ids
    sub.add_parser("migrate-ids",
                   help="One-time: convert numeric ids to hash ids (old id kept as legacy_id; DB backed up first)")

    # export
    pe = sub.add_parser("export", help="Regenerate action_items.md from DB")
    pe.add_argument("--output", metavar="FILE", default=None,
                    help=f"Output path (default: {MD_PATH})")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "init":    cmd_init,
        "list":    cmd_list,
        "ready":   cmd_ready,
        "show":    cmd_show,
        "claim":   cmd_claim,
        "prime":   cmd_prime,
        "add":     cmd_add,
        "update":  cmd_update,
        "append":  cmd_append,
        "done":    lambda a: cmd_done_archive(a, "DONE"),
        "archive": lambda a: cmd_done_archive(a, "ARCHIVED"),
        "migrate-ids": cmd_migrate_ids,
        "export":  cmd_export,
    }
    try:
        dispatch[args.cmd](args)
    except sqlite3.OperationalError as e:
        sys.exit(f"action_items.db is busy/locked — another tracker write may be "
                 f"in progress; retry in a moment. ({e})")
    except sqlite3.DatabaseError as e:
        sys.exit(f"action_items.db is corrupt or has a cloud-sync conflict ({e}). "
                 f"Check for a 'conflicted copy' of the file next to it.")


if __name__ == "__main__":
    main()
