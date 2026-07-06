# itty-bitty-project-tracker

A lightweight, offline-first, cross-project task tracker built on SQLite and vanilla Python. No cloud account, no dependencies beyond Python 3.7+.

**The core idea:** each project keeps its own `action_items.db`; a master "hub" tracker aggregates items from all of them into one prioritized dashboard. Only items with an upcoming deadline or an explicit `[XP]` (cross-project) tag surface in the rollup — everything else stays out of the noise.

```
                  project-A/scripts/todo.py  ──┐
                  project-B/scripts/todo.py  ──┤
                  project-C/scripts/todo.py  ──┤──► rollup.py ──► dashboard.html
    master (this dir)/scripts/todo.py        ──┘                ──► MASTER_PRIORITIES.md
```

---

## Features

- **SQLite backend** — portable, queryable, no ORM overhead
- **Self-contained HTML dashboard** — single file, no server required to *view*; JS+CSS inline; light/dark theme; persistent filter/sort state in `localStorage`
- **Cross-project rollup** — aggregate items across N project directories; only surfaced items appear in the master view
- **`[XP]` tag** — prepend to any item title to force it into the master view regardless of deadline
- **`xp_tags` field** — tag items with project labels for filtering and rollup
- **Add-task modal** — click **+ Add** in the dashboard to add a task to any registered project; falls back to a copy-paste CLI command if the server is not running
- **Inline editing** — click the ✎ button on any row to edit title, owner, deadline, section, status, or XP tags; or mark it Done/Archive — writes through to the source project DB immediately
- **Optimistic concurrency** — stale writes are rejected with a refresh prompt (the row's fingerprint at page-load time must match the live DB state)
- **Dead-simple CLI** — `tracker up / down / restart`; `todo.py add / list / done / …`
- **Zero dependencies** — pure Python stdlib; works on macOS, Linux, Windows WSL

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

You don't need the multi-project hub to get started. If you just want a single task list for yourself (one project, one DB), three commands are enough:

```bash
git clone https://github.com/gnarayan/itty-bitty-project-tracker
cd itty-bitty-project-tracker
cp tracker_config.example.py scripts/tracker_config.py
# Edit scripts/tracker_config.py to set PROJECT_TITLE and your sections

python3 scripts/todo.py init
tracker add "Write the proposal intro"           # short alias; --section defaults to your first section
python3 scripts/rollup.py --html && open dashboard.html
```

The `tracker up` server is **optional** — it only adds the in-browser **+ Add** button. All writes can be done via the `tracker` / `todo.py` CLI. Start without the server; add it if you want the UI shortcut.

---

## Quick start

```bash
git clone https://github.com/gnarayan/itty-bitty-project-tracker
cd itty-bitty-project-tracker

# Set up your master tracker config
cp tracker_config.example.py scripts/tracker_config.py
# (edit scripts/tracker_config.py — see "Configuration" below)

# Initialize the master SQLite DB
python3 scripts/todo.py init

# Add your first task
python3 scripts/todo.py add --section active --title "Read the README"

# Generate and open the dashboard
python3 scripts/rollup.py --html
open dashboard.html              # or: xdg-open dashboard.html on Linux
```

**The `tracker` CLI wrapper** (optional — also enables the in-browser **+ Add** button):

```bash
# Ensure ~/.local/bin is on your PATH (add to ~/.zshrc or ~/.bashrc if needed)
chmod +x launch.sh
ln -s "$(pwd)/launch.sh" ~/.local/bin/tracker

tracker up       # starts server at http://127.0.0.1:8765 and opens browser
tracker down     # stop
tracker restart  # bounce

# Short aliases for common commands (--section defaults to your first section):
tracker add "Read the README"
tracker add --section backlog --title "Future idea"
tracker list
tracker done 3
```

`tracker up` runs the server **detached** (`nohup` + `disown`), so it keeps
running after you close the terminal. It does not print to the terminal; all
output goes to a rotating log:

- **macOS:** `~/Library/Logs/tracker/serve.log`
- **Linux / WSL:** `${XDG_STATE_HOME:-~/.local/state}/tracker/serve.log`

The log rotates automatically and is capped at ~6 MB total (5 × 1 MB backups),
so it never grows without bound. `tracker down` stops the server.

> **macOS note:** `launch.sh` uses Python's `os.path.realpath` for symlink resolution
> (not `readlink -f`, which is GNU-only and absent on stock macOS). No `brew install
> coreutils` required. Set `TRACKER_PORT` env var to use a port other than 8765.

### Optional: start at login / restart on crash (`tracker install`)

On macOS you can register the server as a launchd **LaunchAgent** so it starts
automatically at login and is restarted if it ever crashes:

```bash
tracker install     # generate + load ~/Library/LaunchAgents/com.ittybitty.tracker.plist
tracker uninstall    # stop and remove the agent
```

`tracker install` resolves your actual `python3` (e.g. a conda/pyenv
interpreter) and bakes its absolute path into the plist, since launchd runs with
a minimal `PATH`. On Linux, `tracker install` prints `systemd --user` / cron
`@reboot` pointers instead. On any platform, plain `tracker up` already
backgrounds safely without installing anything.

> **⚠️ Do not `tracker install` when this directory is synced across machines**
> (Dropbox, iCloud Drive, Google Drive, etc.). A server auto-started on every
> machine means **concurrent writers** to `action_items.db` and the Markdown
> files — which produces "conflicted copy" files and risks database corruption.
> For a multi-machine synced setup, skip `install` and just run `tracker up`
> on whichever machine you are currently using.

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
│   └── serve.py              ← localhost server backing the + Add button
├── action_items.db           ← master SQLite DB (gitignored)
├── action_items.md           ← auto-generated markdown view (gitignored)
├── dashboard.html            ← auto-generated dashboard (gitignored)
└── MASTER_PRIORITIES.md      ← auto-generated rollup (gitignored)
```

Generated files (`.db`, `.md`, `.html`) and your personal `scripts/tracker_config.py` are gitignored — they contain your live task data or local paths and are rebuilt / copied on demand.

---

## Configuration

Edit `scripts/tracker_config.py` (copy from `tracker_config.example.py` to start):

```python
PROJECT_TITLE = "My Priorities"

# Sections in the master tracker
SECTION_ORDER = [
    ("active",  "Active Projects"),
    ("backlog", "Backlog"),
    ("watch",   "Standing / Watch"),
]

STANDING_SLUG = "watch"          # items here are filtered out of the rollup

# Projects to aggregate (see "Adding a new project" below)
PROJECTS = [
    ("ProjectA", "~/work/projectA"),          # absolute/~ path
    ("ProjectB", "work/projectB"),            # relative path → resolved via PROJECT_ROOTS
]

# Base directories for relative PROJECTS paths (default: common cloud-sync
# mounts — Dropbox/Box/OneDrive/Drive — plus $HOME; set this to be explicit)
PROJECT_ROOTS = ["~/work"]

ROLLUP_WINDOW_DAYS = 60    # surface items due within this many days
```

---

## Adding a new project

Each project you want to track and aggregate needs:

1. Its own `scripts/todo.py` (copy from this repo)
2. Its own `scripts/tracker_config.py` (copy from `tracker_config.example.py`, set `SECTION_ORDER`)
3. Its own `action_items.db` (created by `python3 scripts/todo.py init`)

Then register it in the **master** `scripts/tracker_config.py`:

```python
PROJECTS = [
    ("ProjectA", "~/work/projectA"),   # absolute path to the project directory
]
```

**Automated (recommended):**

```bash
tracker init-project MyProject ~/work/my-project
```

This copies `todo.py`, creates `scripts/tracker_config.py` (from the example, with `PROJECT_TITLE` pre-filled), and runs `todo.py init` to create `action_items.db`. Then follow the printed instructions to add the entry to the master `PROJECTS` list.

**Manual step by step:**

```bash
# In your project directory:
mkdir -p my-project/scripts
cp /path/to/itty-bitty-project-tracker/scripts/todo.py       my-project/scripts/
cp /path/to/itty-bitty-project-tracker/tracker_config.example.py  my-project/scripts/tracker_config.py

# Edit my-project/scripts/tracker_config.py to set PROJECT_TITLE and SECTION_ORDER
# (remove the PROJECTS / PROJECT_ROOTS entries — those only go in the master hub)

# Initialize the project DB
cd my-project
python3 scripts/todo.py init

# Register in master tracker
# Add to PROJECTS in itty-bitty-project-tracker/scripts/tracker_config.py:
#   ("MyProject", "~/path/to/my-project"),
```

After registering, run `python3 scripts/rollup.py --html` in the master hub to update the dashboard.

**Items from a project surface in the rollup when:**
- Their deadline is within `ROLLUP_WINDOW_DAYS` of today, **or**
- Their title is prefixed with `[XP]` (explicit cross-project tag), **or**
- Their `xp_tags` field is set (comma-separated project labels)

---

## CLI reference

### `tracker` (the shell wrapper)

```
tracker [up]                        Start server (detached; logs to a rotating file), open browser
tracker down                        Stop the server
tracker restart                     Bounce (down + up)
tracker install                     macOS: start at login + restart on crash (launchd). See warning above.
tracker uninstall                   macOS: stop and remove the launchd agent
tracker add "title" [--section S]   Add a task (section defaults to first in SECTION_ORDER)
tracker list [--section S] [--json] List open tasks
tracker done <id>                   Mark task done and move to archive
tracker init-project <label> <path> Scaffold a new project tracker (see below)
```

Set `TRACKER_PORT=NNNN` to use a port other than 8765.

### `todo.py` (per-project task engine)

Run from inside any project directory that has `scripts/todo.py`:

```bash
python3 scripts/todo.py init                        # create action_items.db
python3 scripts/todo.py add \
    --section active \
    --title "Write the proposal intro" \
    --deadline 2026-07-01 \
    --xp "ProjectB,ProjectC"                        # force into rollup with cross-project tags
python3 scripts/todo.py add \
    --section active \
    --title "Monthly report" \
    --deadline 2026-07-01 \
    --recur monthly \
    --priority H                                    # recurring + high-priority
python3 scripts/todo.py add \
    --title "Deploy" \
    --depends 3,7 \
    --snooze 2026-08-01                             # blocked + snoozed
python3 scripts/todo.py list                        # open items (snoozed hidden by default)
python3 scripts/todo.py list --section active       # filter by section
python3 scripts/todo.py list --due-before 2026-08-01
python3 scripts/todo.py list --search "report"     # substring search (title/status/notes)
python3 scripts/todo.py list --snoozed             # only currently-snoozed items
python3 scripts/todo.py list --all                 # includes snoozed
python3 scripts/todo.py show <id>                   # full record
python3 scripts/todo.py update <id> --deadline 2026-07-15
python3 scripts/todo.py update <id> --priority M   # set priority (H/M/L; "" to clear)
python3 scripts/todo.py update <id> --snooze 2026-09-01  # snooze; "" to clear; --wait is alias
python3 scripts/todo.py update <id> --recur 2w     # Nd/Nw/Nm/Ny or daily/weekly/monthly/yearly
python3 scripts/todo.py update <id> --depends 5,9  # prerequisite IDs; "" to clear
python3 scripts/todo.py append <id> --text "Status note"  # dated note appended to status_detail
python3 scripts/todo.py done <id>                   # mark done; recurring items respawn next occurrence
python3 scripts/todo.py archive <id>                # mark archived (recurring items do NOT respawn)
python3 scripts/todo.py export                      # regenerate action_items.md from DB
```

### `rollup.py` (cross-project aggregator)

Run from the master hub directory:

```bash
python3 scripts/rollup.py                           # write MASTER_PRIORITIES.md
python3 scripts/rollup.py --html                    # write dashboard.html
python3 scripts/rollup.py --window-days 30          # narrow the deadline window
python3 scripts/rollup.py --json                    # dump to stdout as JSON
```

---

## The `[XP]` tag and `xp_tags` field

Two ways to force an item into the cross-project rollup regardless of its deadline:

**`[XP]` in the title** (quick, visible):
```bash
python3 scripts/todo.py add --section active --title "[XP] Ship the v1.0 release"
```

**`--xp` flag** (attaches project labels for filtering):
```bash
python3 scripts/todo.py add --section active --title "Review PR" --xp "ProjectA,ProjectB"
python3 scripts/todo.py update <id> --xp "ProjectA"
```

Items with `xp_tags` appear in the dashboard's project filter for those labels and always surface in the rollup.

---

## Priority, snooze, recurrence, and dependencies

### Priority (`--priority H|M|L`)

Attach a priority level when adding or updating any item. Priority sorts after deadline (deadline takes precedence), and `[H]`/`[M]`/`[L]` badges appear in `list` output. High-priority items also surface in the hub rollup even when they have no near deadline. Clear with `--priority ""`.

### Snooze / wait-until (`--snooze YYYY-MM-DD` / `--wait YYYY-MM-DD`)

Hides an item from the default `list` view until the given date — useful for tasks you cannot act on until a future event. Snoozed items are suppressed from the hub rollup regardless of deadline or priority. Use `list --snoozed` to see only currently-snoozed items; `list --all` includes them in the normal view. `--wait` is an exact alias for `--snooze`. Clear with `--snooze ""`.

### Recurring deadlines (`--recur RULE`)

Attaches a recurrence rule to a task with a deadline. Accepted forms: `daily`, `weekly`, `monthly`, `yearly`, or `Nd`/`Nw`/`Nm`/`Ny` (e.g. `2w`, `3m`). When you mark a recurring item **done**, it respawns as a new open item with the next occurrence date (month-end dates are clamped to the last day of the target month). `archive` does **not** respawn — use it to permanently retire a recurring task. `--recur` requires `--deadline` and exits non-zero without one. Clear with `--recur ""`.

### Task dependencies (`--depends ID[,ID]`)

Records prerequisite item IDs (comma-separated). This is **informational** — it does not block writes or prevent completion. Unmet dependencies show as a `🔒 blocked by: #X` badge in `list` output; the badge disappears automatically once the prerequisite item is closed (deleted from the DB). Clear with `--depends ""`.

---

## Editing tasks in the dashboard

When the server is running, each task row shows a **✎** button on hover. Clicking it opens an edit modal with the task's current values pre-filled.

**Editable fields:** Title, Owner, Deadline, Section, Status tag, Cross-project tags.

**Done / Archive buttons:** mark the task closed; it is **permanently deleted from the DB** and appended as a markdown row to the project's `action_items_archive.md`. This is irreversible from the DB side — recovery requires hand-editing the archive and re-adding. Double-check the ID before clicking.

**Project is locked** (display-only). Moving a task between projects means moving it between SQLite files; use `todo.py done <id>` and `todo.py add` in the target project instead.

**Optimistic concurrency:** the page embeds a fingerprint of each task's fields at render time. When you submit an edit, the server recomputes the fingerprint from the live DB and rejects the write with a "refresh" prompt if the task was modified since you loaded the page (e.g. by a concurrent CLI edit or another browser tab). This prevents silent overwrites.

**When the server is not running:** the edit modal shows a copy-paste `todo.py update` command instead, matching the same fallback behaviour as the Add modal.

---

## Security notes

The server (`serve.py`) is localhost-only (`127.0.0.1:8765`). It is not intended to be exposed to a network. Key properties:

- All SQL queries use parameterized statements — no injection risk
- User-supplied content is HTML-escaped before rendering in text/content positions (tags `& < > " '` are escaped)
- Project and section inputs are validated against a live whitelist before writing
- Subprocesses use argument lists (`shell=False`) — no shell injection
- **CSRF / DNS-rebinding protection:** write endpoints (`/api/add`, `/api/update`, `/api/done`) require (1) a `Host` header matching `127.0.0.1:PORT` or `localhost:PORT`, (2) an `Origin` header (when present) that matches, and (3) a custom `X-Tracker` header that cross-origin requests cannot set without a CORS preflight (which the server does not grant). This prevents a malicious web page from forging write requests.
- Writes are serialized in-process via a threading lock; `PRAGMA busy_timeout=5000` handles cross-process contention from concurrent CLI edits or rollup reads
- **Write locking note:** the DB uses SQLite's default rollback-journal mode (not WAL). WAL mode is faster for concurrent readers but unsafe on network-synced filesystems (Dropbox, NFS). If you are on a local-only filesystem and have many concurrent readers, you can enable WAL with `PRAGMA journal_mode=WAL` after running `todo.py init`
- **Multi-machine / Dropbox note:** two machines editing the same `action_items.db` simultaneously can produce a Dropbox conflict copy (`.db` is a binary file; Dropbox cannot merge it). Treat each project DB as single-writer at a time. The `busy_timeout` handles concurrent *processes* on one machine; it does not protect against two-machine edits.
- No authentication (relies on localhost isolation)

---

## Schema

One table per SQLite DB (`action_items.db`):

```sql
CREATE TABLE items (
    raw_id       TEXT PRIMARY KEY,
    sort_id      INTEGER,
    section      TEXT NOT NULL,
    title        TEXT NOT NULL,
    owner        TEXT,
    source_date  TEXT,
    deadline     TEXT,          -- ISO YYYY-MM-DD
    status_tag   TEXT,          -- normalized: OPEN, IN PROGRESS, DONE, …
    status_emoji TEXT,
    is_owner     INTEGER DEFAULT 0,
    is_standing  INTEGER DEFAULT 0,
    status_detail TEXT,         -- free-text; dated notes appended here
    xp_tags      TEXT,          -- comma-separated cross-project labels
    priority     TEXT,          -- H / M / L or NULL
    wait_until   TEXT,          -- ISO YYYY-MM-DD; snooze hide-until date
    recur        TEXT,          -- recurrence rule (e.g. weekly, 2w, monthly)
    depends_on   TEXT           -- comma-separated prerequisite item IDs
);
```

Existing `action_items.db` files are **upgraded automatically**: `open_db()` runs
`ALTER TABLE … ADD COLUMN` for any column that is absent — no manual migration step needed.

---

## Portability notes

- **macOS / Linux / Windows WSL:** all supported. `launch.sh` auto-detects the right `open`/`xdg-open`/`wslview` browser opener — no manual edits needed.
- **Symlink resolution:** `launch.sh` uses Python's `os.path.realpath` (not `readlink -f`), so it works on stock macOS without `brew install coreutils`.
- **Custom port:** set `TRACKER_PORT=NNNN` in your shell environment to run on a port other than 8765.
- **Default paths:** if `PROJECT_ROOTS` is not set, rollup.py searches the common cloud-sync mounts (the macOS `~/Library/CloudStorage/*` File Provider dirs — Dropbox, Box, OneDrive, Google Drive — plus `~/Dropbox`, `~/Box`, `~/OneDrive`, `~/Google Drive`) and finally `$HOME`. Set `PROJECT_ROOTS` in `tracker_config.py` to be explicit or for any other layout.
- **PATH:** `~/.local/bin` is the recommended location for the `tracker` symlink. Ensure it is on your `$PATH` (add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc` if needed).

---

## License

MIT
