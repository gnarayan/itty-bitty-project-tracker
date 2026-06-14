#!/usr/bin/env python3
"""
Thin CLI for action_items.db — parameterized reads/writes so escaping is never a concern.
Self-contained engine; per-project customisation lives in tracker_config.py (sibling file).

Subcommands:
  init    Create an empty action_items.db in the project root (run once at setup)
  list    [--all] [--owner] [--section S] [--status TAG] [--due-before DATE]
          [--standing] [--limit N] [--json]
  show    <id>
  add     --section S --title "..." [--owner ...] [--deadline ...] [--status "..."|--status-file FILE]
  update  <id> [--title ...] [--owner ...] [--deadline ...] [--tag ...] [--status "..."|--status-file -]
  append  <id> [--text "..."|--text-file -]
  done    <id>
  archive <id>
  export  [--output FILE]   (default: action_items.md next to action_items.db)
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import date
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
    for kw in keywords:
        if clean.startswith(kw):
            return kw
    for kw in keywords:
        if kw in clean[:60]:
            return kw
    return "OPEN"


def extract_deadline(text):
    m = re.search(r'(?:deadline|due\s*(?:date)?)[:\s]+(\d{4}-\d{2}-\d{2})', text, re.I)
    if m:
        return m.group(1)
    m = re.search(r'\*\*[^*]*?(20\d{2}-\d{2}-\d{2})[^*]*?\*\*', text)
    if m:
        return m.group(1)
    return None


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
    xp_tags       TEXT
);
CREATE INDEX IF NOT EXISTS idx_status   ON items(status_tag);
CREATE INDEX IF NOT EXISTS idx_section  ON items(section);
CREATE INDEX IF NOT EXISTS idx_owner    ON items(is_owner);
CREATE INDEX IF NOT EXISTS idx_deadline ON items(deadline);
"""

# Columns added after the initial schema (applied to pre-existing DBs by an
# external migration step). New DBs from `init` already include them via _SCHEMA.
_MIGRATIONS = [
    ("xp_tags", "TEXT"),
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
    return conn


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

    if not args.all and not args.standing:
        closed = ",".join(f"'{t}'" for t in CLOSED_TAGS)
        conditions.append(f"status_tag NOT IN ({closed})")
        conditions.append("is_standing = 0")
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

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT raw_id, title, owner, deadline, status_emoji, status_tag, is_owner, section
        FROM items
        {where}
        ORDER BY
            CASE WHEN deadline IS NULL THEN '9999' ELSE deadline END,
            is_owner DESC,
            CAST(SUBSTR(raw_id, 1, INSTR(raw_id||'x','x')-1) AS INTEGER)
        LIMIT ?
    """
    params.append(args.limit)
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return

    if not rows:
        print("(no items)")
        return

    header = f"{'#':<6} {'Title':<55} {'Owner':<20} {'Deadline':<11} {'Status'}"
    print(header)
    print("-" * len(header))
    for r in rows:
        title_short = re.sub(r'\*\*', '', r['title'])[:55]
        owner_short = (r['owner'] or '')[:20]
        deadline    = r['deadline'] or '—'
        tag         = (r['status_emoji'] or '') + (r['status_tag'] or '')
        marker      = "★" if (r['is_owner'] and OWNER_TAG_VARIANTS) else " "
        print(f"{marker}{r['raw_id']:<5} {title_short:<55} {owner_short:<20} {deadline:<11} {tag}")

    print(f"\n({len(rows)} items shown)")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def cmd_show(args):
    conn = open_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM items WHERE raw_id = ?", (args.id,))
    row = cur.fetchone()
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

    status_text = read_status_arg(args) or "OPEN"
    tag, deadline, emoji, _ = derive_fields(status_text, args.title, args.tag, args.deadline)
    owner_flag  = 1 if is_owner_item(args.title) else 0
    is_standing = 1 if args.section == STANDING_SLUG else 0
    today       = date.today().isoformat()

    conn = open_db()
    cur  = conn.cursor()
    # Acquire write lock before computing new ID so no concurrent CLI add
    # can claim the same sort_id between our SELECT MAX and INSERT.
    conn.execute("BEGIN IMMEDIATE")
    cur.execute("SELECT MAX(sort_id) FROM items")
    max_sort = cur.fetchone()[0] or 0
    new_sort = max_sort + 1
    new_id   = str(new_sort)

    xp_tags = getattr(args, 'xp', None) or None

    cur.execute("""
        INSERT INTO items
        (raw_id, sort_id, section, title, owner, source_date, deadline,
         status_tag, status_emoji, is_owner, is_standing, status_detail, xp_tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_id, new_sort, args.section, args.title,
          args.owner or "", today, deadline,
          tag, emoji, owner_flag, is_standing, status_text, xp_tags))
    conn.commit()
    conn.close()
    print(f"Added item #{new_id} in section '{args.section}'")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def cmd_update(args):
    conn = open_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM items WHERE raw_id = ?", (args.id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")

    updates = {}

    new_status = read_status_arg(args)
    if new_status is not None:
        updates['status_detail'] = new_status
        tag, deadline, emoji, _ = derive_fields(new_status, row['title'], args.tag, args.deadline)
        updates['status_tag']   = tag
        updates['deadline']     = deadline
        updates['status_emoji'] = emoji

    if args.title is not None:
        updates['title']    = args.title
        updates['is_owner'] = 1 if is_owner_item(args.title) else 0
    if args.owner is not None:
        updates['owner'] = args.owner
    if args.deadline is not None:
        updates['deadline'] = args.deadline
    if args.tag is not None:
        updates['status_tag'] = args.tag.upper()
    if args.section is not None:
        if args.section not in SLUG_TO_DISPLAY:
            conn.close()
            sys.exit(f"Unknown section '{args.section}'")
        updates['section']     = args.section
        updates['is_standing'] = 1 if args.section == STANDING_SLUG else 0

    xp_val = getattr(args, 'xp', None)
    if xp_val is not None:
        updates['xp_tags'] = xp_val if xp_val else None

    if not updates:
        conn.close()
        print("Nothing to update.")
        return

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [args.id]
    cur.execute(f"UPDATE items SET {set_clause} WHERE raw_id = ?", vals)
    conn.commit()
    conn.close()
    print(f"Updated item #{args.id} ({', '.join(updates.keys())})")


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------

def cmd_append(args):
    text = read_text_arg(args)
    if not text:
        sys.exit("Provide --text or --text-file")

    conn = open_db()
    cur  = conn.cursor()
    cur.execute("SELECT status_detail, title, deadline FROM items WHERE raw_id = ?", (args.id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")

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
    """, (new_detail, tag, deadline, emoji, args.id))
    conn.commit()
    conn.close()
    print(f"Appended to item #{args.id}")


# ---------------------------------------------------------------------------
# done / archive
# ---------------------------------------------------------------------------

def cmd_done_archive(args, final_tag):
    conn = open_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM items WHERE raw_id = ?", (args.id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        sys.exit(f"No item with id '{args.id}'")

    today       = date.today().isoformat()
    status_text = row['status_detail'] or ''
    if not status_text.upper().startswith(final_tag):
        status_text = f"**{final_tag} {today}** — {status_text}"

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
    archive_text += archive_row + "\n"
    ARCHIVE_PATH.write_text(archive_text, encoding='utf-8')

    cur.execute("DELETE FROM items WHERE raw_id = ?", (args.id,))
    conn.commit()
    conn.close()
    print(f"Item #{args.id} archived to {ARCHIVE_PATH.name}")

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
            "SELECT * FROM items WHERE section = ? ORDER BY sort_id,"
            " CAST(SUBSTR(raw_id,1,INSTR(raw_id||'x','x')-1) AS INTEGER)",
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
    pl.add_argument("--limit",      type=int, default=500, metavar="N")
    pl.add_argument("--json",       action="store_true", help="JSON output")

    # show
    ps = sub.add_parser("show", help="Show full record for one item")
    ps.add_argument("id", metavar="ID")

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
        "show":    cmd_show,
        "add":     cmd_add,
        "update":  cmd_update,
        "append":  cmd_append,
        "done":    lambda a: cmd_done_archive(a, "DONE"),
        "archive": lambda a: cmd_done_archive(a, "ARCHIVED"),
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
