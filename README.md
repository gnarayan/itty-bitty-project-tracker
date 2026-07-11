# itty-bitty-project-tracker

A lightweight, offline-first, cross-project task tracker built on SQLite and vanilla Python. No cloud account, no dependencies beyond Python 3.7+.

**The core idea:** each project keeps its own `action_items.db`; a master "hub" tracker aggregates items from all of them into one prioritized dashboard. Only items with an upcoming deadline or an explicit `[XP]` (cross-project) tag surface in the rollup — everything else stays out of the noise.

```
                  project-A/scripts/todo.py  ──┐
                  project-B/scripts/todo.py  ──┤
                  project-C/scripts/todo.py  ──┤──► rollup.py ──► dashboard.html
    master (this dir)/scripts/todo.py        ──┘                ──► MASTER_PRIORITIES.md
```

**Docs:** this README gets you set up and covers daily use. [REFERENCE.md](REFERENCE.md) has every flag, feature semantics, the dashboard in detail, security, and the schema. [AGENTS.md](AGENTS.md) is for AI coding assistants working in a tracked directory.

---

## Comparison to alternatives

| Tool | Offline | Multi-project rollup | HTML dashboard | Dependencies |
|---|---|---|---|---|
| **itty-bitty-project-tracker** | ✅ | ✅ | ✅ | None (stdlib) |
| Taskwarrior | ✅ | ⚠️ manual | ❌ | C++ binary |
| todo.txt | ✅ | ❌ | ❌ | None |
| org-mode | ✅ | ⚠️ with effort | ❌ | Emacs |
| GitHub Projects / Linear | ❌ | ✅ | ✅ | Account + internet |
| Obsidian Tasks | ✅ | ⚠️ | ⚠️ | Obsidian |
| Notion | ❌ | ✅ | ✅ | Account + subscription |
| Things 3 | ✅ | ❌ | ❌ | macOS/iOS only, paid |

**Sweet spot:** you have 3–10 active projects, each with its own directory / repo, and you want one place to see what's actually due next week across all of them — without leaving the terminal or signing into anything.

---

## Just me, one list

You don't need the multi-project hub to get started. For a single task list, three commands are enough:

```bash
git clone https://github.com/gnarayan/itty-bitty-project-tracker
cd itty-bitty-project-tracker
cp tracker_config.example.py scripts/tracker_config.py
# Edit scripts/tracker_config.py to set PROJECT_TITLE and your sections

python3 scripts/todo.py init
python3 scripts/todo.py add --title "Write the proposal intro"
python3 scripts/rollup.py --html && open dashboard.html
```

The server is **optional** — it only enables in-browser add/edit. All writes work through the CLI.

---

## Quick start (hub + projects)

```bash
git clone https://github.com/gnarayan/itty-bitty-project-tracker
cd itty-bitty-project-tracker

cp tracker_config.example.py scripts/tracker_config.py
# edit scripts/tracker_config.py — see "Configuration" below

python3 scripts/todo.py init                                     # master DB
python3 scripts/todo.py add --section active --title "Read the README"
python3 scripts/rollup.py --html
open dashboard.html                                              # xdg-open on Linux
```

**The `tracker` CLI wrapper** (optional; also enables in-browser add/edit):

```bash
chmod +x launch.sh
ln -s "$(pwd)/launch.sh" ~/.local/bin/tracker    # ~/.local/bin must be on $PATH

tracker up        # server at http://127.0.0.1:8765, detached; logs to a rotating file
tracker down
tracker add "Read the README"
tracker list
tracker done 3
```

macOS users can register the server to start at login with `tracker install` —
**but never on a directory synced across machines** (Dropbox/iCloud/Drive):
auto-started servers on two machines mean concurrent writers to
`action_items.db` and risk a corrupting conflict copy. On synced setups, run
`tracker up` on the machine you're using. Details: [REFERENCE.md](REFERENCE.md#tracker-the-shell-wrapper).

---

## Daily use

The handful of commands that cover most days (full flag reference: [REFERENCE.md](REFERENCE.md#cli-reference)):

```bash
python3 scripts/todo.py list                          # what's open (snoozed hidden)
python3 scripts/todo.py add --title "..." --deadline 2026-07-01
python3 scripts/todo.py append 12 --text "sent draft" # dated status note
python3 scripts/todo.py update 12 --deadline 2026-07-15
python3 scripts/todo.py done 12                       # close + archive (irreversible)
```

Useful extras, one flag each: `--priority H|M|L`, `--snooze YYYY-MM-DD`,
`--recur weekly|2w|monthly|…` (respawns on `done`), `--depends 3,7`,
`--xp "ProjectA"` or an `[XP]` title prefix to force an item into the
cross-project rollup. Semantics: [REFERENCE.md](REFERENCE.md#feature-semantics).

**The dashboard** (`python3 scripts/rollup.py --html`, or just load
`http://127.0.0.1:8765` when the server is up) shows everything surfaced across
projects, with search (text or refs like `ProjectA#12`), filters, markdown in
status notes, and — when served — inline add/edit/done. Details:
[REFERENCE.md](REFERENCE.md#the-dashboard).

---

## Directory layout

```
itty-bitty-project-tracker/   ← master tracker hub (this repo)
├── launch.sh                 ← tracker CLI wrapper
├── scripts/
│   ├── tracker_config.example.py  ← template config (tracked in git)
│   ├── tracker_config.py     ← YOUR config (copy from example; gitignored)
│   ├── todo.py               ← per-project CRUD engine
│   ├── rollup.py             ← cross-project aggregator
│   └── serve.py              ← localhost server backing in-browser add/edit
├── action_items.db           ← master SQLite DB (gitignored)
├── action_items.md           ← auto-generated markdown view (gitignored)
├── dashboard.html            ← auto-generated dashboard (gitignored)
└── MASTER_PRIORITIES.md      ← auto-generated rollup (gitignored)
```

Generated files (`.db`, `.md`, `.html`) and your personal `scripts/tracker_config.py` are gitignored — they contain your live task data or local paths.

---

## Configuration

Edit `scripts/tracker_config.py` (copy from `tracker_config.example.py`):

```python
PROJECT_TITLE = "My Priorities"

SECTION_ORDER = [
    ("active",  "Active Projects"),
    ("backlog", "Backlog"),
    ("watch",   "Standing / Watch"),
]

STANDING_SLUG = "watch"          # items here are filtered out of the rollup

# Projects to aggregate (hub only)
PROJECTS = [
    ("ProjectA", "~/work/projectA"),          # absolute/~ path
    ("ProjectB", "work/projectB"),            # relative → resolved via PROJECT_ROOTS
]

PROJECT_ROOTS = ["~/work"]       # base dirs for relative PROJECTS paths

ROLLUP_WINDOW_DAYS = 60          # surface items due within this many days
```

---

## Adding a new project

**Automated (recommended):**

```bash
tracker init-project MyProject ~/work/my-project
```

This copies `todo.py`, creates the project's `scripts/tracker_config.py`, and
initializes its `action_items.db`. Then add the printed entry to `PROJECTS` in
the **master** config and run `python3 scripts/rollup.py --html` in the hub.

**Manually:** copy `scripts/todo.py` and `tracker_config.example.py` (as
`scripts/tracker_config.py`, without the `PROJECTS`/`PROJECT_ROOTS` entries —
those belong only in the hub) into the project, run `python3 scripts/todo.py
init`, and register the path in the master `PROJECTS` list.

Items from a project surface in the hub rollup when their deadline falls within
`ROLLUP_WINDOW_DAYS`, their title carries `[XP]`, their `xp_tags` is set, or
their priority is `H` ([details](REFERENCE.md#feature-semantics)).

When you update this repo, re-copy `scripts/todo.py` into each project — DBs
migrate themselves, engines don't ([upgrading](REFERENCE.md#upgrading-sub-projects)).

---

## License

MIT
