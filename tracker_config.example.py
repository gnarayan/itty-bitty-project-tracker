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
# Defaults to macOS Dropbox locations. Set this if your projects live elsewhere:
#   PROJECT_ROOTS = ["~/work", "/mnt/shared/projects"]
# PROJECT_ROOTS = None

# Surface project items due within this many days (or flagged [XP] in title).
ROLLUP_WINDOW_DAYS = 60
