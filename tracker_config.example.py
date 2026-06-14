# tracker_config.example.py — copy to scripts/tracker_config.py and customize
#
# This is the configuration for the MASTER tracker (the cross-project rollup hub).
# Each project you want to monitor also needs its own scripts/tracker_config.py
# inside its directory (see README.md § Adding a new project).

# ── Master tracker identity ───────────────────────────────────────────────────

PROJECT_TITLE = "My Priorities"

INTRO_BLURB = (
    "Cross-project priorities hub. "
    "Items from project trackers surface via rollup.py "
    "(deadline ≤ ROLLUP_WINDOW_DAYS or [XP]-tagged). "
    "Regenerate dashboard: python3 scripts/rollup.py --html"
)

# Sections in THIS (master) tracker — edit freely.
# Each entry is (slug, display_label).
SECTION_ORDER = [
    ("active",   "Active Projects"),
    ("backlog",  "Backlog"),
    ("watch",    "Standing / Watch"),
]

STANDING_SLUG = "watch"     # items in this section are not surfaced in rollup

# ── Rollup: project trackers to aggregate ────────────────────────────────────
# PROJECTS: list of (label, path) pairs. Each path can be:
#   - An absolute or ~/… path:   "~/work/myproject"
#   - A relative path:           "work/myproject"   (resolved via PROJECT_ROOTS below)
# rollup.py looks for action_items.db inside each resolved directory.
PROJECTS = [
    # ("ProjectA", "~/work/projectA"),
    # ("ProjectB", "work/projectB"),      # relative to PROJECT_ROOTS
]

# PROJECT_ROOTS: base directories for resolving relative PROJECTS paths above.
# Defaults to the common cloud-sync mounts (Dropbox/Box/OneDrive/Drive) and $HOME.
# Set this if your projects live elsewhere, e.g.:
#   PROJECT_ROOTS = ["~/work", "/mnt/shared/projects"]   # custom layout
#   PROJECT_ROOTS = ["~/Library/CloudStorage/Box-Box"]   # Box on macOS
# PROJECT_ROOTS = None

# Surface project items due within this many days (or flagged [XP] in title).
ROLLUP_WINDOW_DAYS = 60

# ── Status taxonomy (optional) ───────────────────────────────────────────────
# All of the following have sensible defaults; uncomment to tailor them.
# The dashboard's status filters are built dynamically from whatever tags exist
# in the database, so any free-form `todo.py --tag VALUE` always shows up — these
# settings only affect auto-extraction and which tags count as "closed".

# STATUS_KEYWORDS: recognised when auto-deriving a tag from free-text status.
# Keep "OPEN" (the fallback) and "DONE"/"ARCHIVED" (set by done/archive).
# STATUS_KEYWORDS = [
#     "IN PROGRESS", "BLOCKED", "ON HOLD", "QUEUED",
#     "TODO", "ACTIVE", "OPEN", "DONE", "ARCHIVED", "CANCELLED",
# ]

# CLOSED_TAGS: tags treated as closed — hidden from active list/rollup views.
# Keep this in sync with STATUS_KEYWORDS. todo.py and rollup.py both read it.
# CLOSED_TAGS = frozenset(["DONE", "ARCHIVED", "CANCELLED"])

# OWNER_TAG_VARIANTS: title substrings that flag a "personal owner" item (★).
# Leave empty (default) to disable the --owner flag and the ★ marker.
# OWNER_TAG_VARIANTS = ["[ME]", "[MINE]"]
# OWNER_TAG_LABEL    = "Owner"     # shown in --help text

# EMOJI_SET: leading status emoji recognised at the start of a status line.
# EMOJI_SET = "✅🔴🟡⚠️🟢⏳"
