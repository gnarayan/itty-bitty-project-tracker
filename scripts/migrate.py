#!/usr/bin/env python3
"""
One-time migration: existing action_items.md (markdown table) → action_items.db.

Usage:
  python3 scripts/migrate.py [--dry-run]

The script reads tracker_config.py (sibling file) for the section taxonomy
and the COLUMN_MAP that tells it which pipe-delimited column index holds
each field in this project's markdown tables.

COLUMN_MAP keys (0-indexed, after the leading | is stripped):
  "id"      — row number / item ID  (required)
  "title"   — action text           (required)
  "owner"   — owner(s)              (optional, default col 3)
  "source"  — source date           (optional, default col 4)
  "status"  — status text           (optional, default col 5)
  "tag"     — extra tag/label col   (optional; if present, prepended to title as [TAG])

Example for DESC-style tables  (| # | Action | Owner(s) | Source date | Status |):
  COLUMN_MAP = {"id": 0, "title": 1, "owner": 2, "source": 3, "status": 4}

Example for Advising-style tables  (| # | Tag | Item | Source | Status |):
  COLUMN_MAP = {"id": 0, "tag": 1, "title": 2, "source": 3, "status": 4}

Set COLUMN_MAP in tracker_config.py before running this script.
"""
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

try:
    import tracker_config as _cfg
except ImportError:
    sys.exit("tracker_config.py not found next to migrate.py. See migrate.py docstring.")

# Import schema, migrations, and parse helpers from the single source of truth
from todo import _SCHEMA, _MIGRATIONS, _emoji_tokens, extract_emoji, extract_status_tag, extract_deadline  # noqa: E402

# Pull constants from config
SECTION_ORDER  = getattr(_cfg, "SECTION_ORDER", [])
SECTION_MAP    = getattr(_cfg, "SECTION_MAP",   {d: s for s, d in SECTION_ORDER})
STANDING_SLUG  = getattr(_cfg, "STANDING_SLUG", "standing_watch")
OWNER_VARIANTS = getattr(_cfg, "OWNER_TAG_VARIANTS", [])
COLUMN_MAP     = getattr(_cfg, "COLUMN_MAP", {"id": 0, "title": 1, "owner": 2, "source": 3, "status": 4})

def _is_owner(title):
    return any(tag in title for tag in OWNER_VARIANTS)


STATUS_KEYWORDS = getattr(_cfg, "STATUS_KEYWORDS", [
    "NEARLY DONE", "IN PROGRESS", "WINDING DOWN", "LOW PRIORITY",
    "QUEUED", "MONITORING", "ACTIVE", "OPEN", "SENT", "DONE",
    "ARCHIVED", "SUPERSEDED", "STANDING", "DE-ESCALATED", "HELD",
])
EMOJI_SET    = getattr(_cfg, "EMOJI_SET", "✅🔴🟡⚠️🟢⏳")
# Pre-tokenized longest-first so ⚠️ (U+26A0 + U+FE0F) matches whole.
EMOJI_TOKENS = sorted(_emoji_tokens(EMOJI_SET), key=len, reverse=True)


BASE         = _HERE.parent
DB_PATH      = BASE / "action_items.db"
MD_PATH      = BASE / "action_items.md"

def slug_for_header(header):
    for name, slug in SECTION_MAP.items():
        if name.lower() in header.lower() or header.lower() in name.lower():
            return slug, slug == STANDING_SLUG
    return re.sub(r'[^a-z0-9]+', '_', header.lower()).strip('_'), False


def parse_row(line):
    """Parse a markdown table row into a list of stripped cell values."""
    parts = line.split('|')
    if len(parts) < 4:
        return None
    cells = [p.strip() for p in parts[1:-1]]
    return cells if len(cells) >= 3 else None


def get_cell(cells, key, fallback=""):
    idx = COLUMN_MAP.get(key)
    if idx is None:
        return fallback
    return cells[idx] if idx < len(cells) else fallback


def ensure_columns():
    """Add any missing columns from _MIGRATIONS to an existing DB. Idempotent."""
    if not DB_PATH.exists():
        sys.exit(f"action_items.db not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("PRAGMA table_info(items)")
    existing = {row[1] for row in cur.fetchall()}
    added = []
    for col_name, col_type in _MIGRATIONS:
        if col_name not in existing:
            cur.execute(f"ALTER TABLE items ADD COLUMN {col_name} {col_type}")
            added.append(col_name)
    conn.commit()
    conn.close()
    if added:
        print(f"Added columns to {DB_PATH}: {', '.join(added)}")
    else:
        print(f"No new columns needed for {DB_PATH} (already up to date)")


def main():
    if "--ensure-columns" in sys.argv:
        ensure_columns()
        return

    dry_run = "--dry-run" in sys.argv

    if not MD_PATH.exists():
        sys.exit(f"action_items.md not found: {MD_PATH}")
    if DB_PATH.exists():
        sys.exit(f"action_items.db already exists: {DB_PATH}\n  Delete it first if you want to re-migrate.")

    text  = MD_PATH.read_text(encoding='utf-8')
    lines = text.splitlines()

    items        = []   # list of dicts to insert
    current_slug = None
    is_standing  = False
    sort_counter = 0
    id_set       = set()

    for line in lines:
        # Detect section headers
        m = re.match(r'^#{1,3}\s+(.+)', line)
        if m:
            header = m.group(1).strip()
            # Skip known non-section headers (title line, etc.)
            if header.endswith("— Open Action Items") or header.lower() in ("archived items", "archived via cli"):
                current_slug = None
                continue
            slug, standing = slug_for_header(header)
            current_slug = slug
            is_standing  = standing
            print(f"  section: {header!r} → {slug} (standing={standing})")
            continue

        if current_slug is None:
            continue

        # Skip separator / header rows
        if re.match(r'^\|[-| ]+\|', line) or re.match(r'^\| #', line, re.I):
            continue

        cells = parse_row(line)
        if cells is None:
            continue

        raw_id = get_cell(cells, "id").lstrip('#').strip()
        if not raw_id or not re.match(r'[\w]+', raw_id):
            continue

        title = get_cell(cells, "title")
        if not title or title in ("-", "—", "..."):
            continue

        # Optional tag column — prepend as [TAG] to title if present
        tag_cell = get_cell(cells, "tag", "")
        if tag_cell and tag_cell not in ("—", "-", ""):
            # Only prepend if not already present
            bracket = f"[{tag_cell}]"
            if bracket not in title:
                title = f"{bracket} {title}"

        owner   = get_cell(cells, "owner")
        source  = get_cell(cells, "source")
        status  = get_cell(cells, "status")

        # Deduplicate IDs (markdown may repeat numbers across sections)
        base_id = raw_id
        suffix  = 0
        while raw_id in id_set:
            suffix += 1
            raw_id = f"{base_id}s{suffix}"
        id_set.add(raw_id)

        sort_counter += 1
        tag      = extract_status_tag(status, STATUS_KEYWORDS)
        deadline = extract_deadline(status)
        emoji    = extract_emoji(status, EMOJI_TOKENS)
        owner_f  = 1 if _is_owner(title) else 0
        stand_f  = 1 if is_standing else 0

        items.append({
            "raw_id":       raw_id,
            "sort_id":      sort_counter,
            "section":      current_slug,
            "title":        title,
            "owner":        owner,
            "source_date":  source,
            "deadline":     deadline,
            "status_tag":   tag,
            "status_emoji": emoji,
            "is_owner":     owner_f,
            "is_standing":  stand_f,
            "status_detail": status,
        })

    print(f"\nParsed {len(items)} items.")

    if dry_run:
        for it in items:
            print(f"  {it['raw_id']:>5} [{it['section']:20}] {it['title'][:60]}")
        print("\nDry run — no DB written.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    for it in items:
        conn.execute("""
            INSERT OR REPLACE INTO items
            (raw_id, sort_id, section, title, owner, source_date, deadline,
             status_tag, status_emoji, is_owner, is_standing, status_detail)
            VALUES (:raw_id, :sort_id, :section, :title, :owner, :source_date,
                    :deadline, :status_tag, :status_emoji, :is_owner, :is_standing,
                    :status_detail)
        """, it)
    conn.commit()
    conn.close()
    print(f"Written: {DB_PATH}")


if __name__ == "__main__":
    main()
