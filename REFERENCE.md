# Reference

The complete reference for itty-bitty-project-tracker: every CLI flag, feature
semantics, the dashboard, security, schema, and portability. For orientation and
setup, see [README.md](README.md). For AI-agent guidance, see [AGENTS.md](AGENTS.md).

---

## CLI reference

### `tracker` (the shell wrapper)

```
tracker [up]                        Start server (detached; logs to a rotating file), open browser
tracker down                        Stop the server
tracker restart                     Bounce (down + up)
tracker install                     macOS: start at login + restart on crash (launchd). See warning below.
tracker uninstall                   macOS: stop and remove the launchd agent
tracker add "title" [--section S]   Add a task (section defaults to first in SECTION_ORDER)
tracker list [--section S] [--json] List open tasks
tracker done <id>                   Mark task done and move to archive
tracker init-project <label> <path> Scaffold a new project tracker
```

Set `TRACKER_PORT=NNNN` to use a port other than 8765.

`tracker up` runs the server **detached** (`nohup` + `disown`), so it survives
closing the terminal. Output goes to a rotating log capped at ~6 MB total:

- **macOS:** `~/Library/Logs/tracker/serve.log`
- **Linux / WSL:** `${XDG_STATE_HOME:-~/.local/state}/tracker/serve.log`

`tracker install` registers a macOS launchd LaunchAgent
(`~/Library/LaunchAgents/com.ittybitty.tracker.plist`) so the server starts at
login and restarts on crash. It resolves your actual `python3` (conda/pyenv
included) and bakes the absolute path into the plist, since launchd runs with a
minimal `PATH`. On Linux it prints `systemd --user` / cron `@reboot` pointers.

> **⚠️ Do not `tracker install` when the tracker directory is synced across
> machines** (Dropbox, iCloud Drive, Google Drive, …). A server auto-started on
> every machine means concurrent writers to `action_items.db`, which produces
> "conflicted copy" files and risks database corruption. On a synced setup, run
> `tracker up` only on the machine you are currently using.

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
python3 scripts/todo.py list --search "report"      # substring search (title/status/notes; % and _ are literal)
python3 scripts/todo.py list --snoozed              # only currently-snoozed items
python3 scripts/todo.py list --all                  # includes snoozed and closed-tagged
python3 scripts/todo.py list --standing             # standing/watch items only
python3 scripts/todo.py list --json                 # machine-readable output
python3 scripts/todo.py show <id>                   # full record
python3 scripts/todo.py update <id> --deadline 2026-07-15   # ISO-validated; "" clears
python3 scripts/todo.py update <id> --tag "IN PROGRESS"     # set status tag
python3 scripts/todo.py update <id> --priority M    # H/M/L; "" to clear
python3 scripts/todo.py update <id> --snooze 2026-09-01     # "" to clear; --wait is alias
python3 scripts/todo.py update <id> --recur 2w      # Nd/Nw/Nm/Ny or daily/weekly/monthly/yearly; "" clears
python3 scripts/todo.py update <id> --depends 5,9   # prerequisite IDs; "" to clear
python3 scripts/todo.py update <id> --status "text" # replace status detail
python3 scripts/todo.py update <id> --status-file - # multi-line status via stdin
python3 scripts/todo.py append <id> --text "Status note"  # dated note appended to status_detail
python3 scripts/todo.py done <id>                   # mark done; recurring items respawn next occurrence
python3 scripts/todo.py archive <id>                # mark archived (recurring items do NOT respawn)
python3 scripts/todo.py export                      # regenerate action_items.md from DB
```

Item ids are **never reused**: closing an item retires its id permanently (a
monotone counter in the `meta` table), so archive rows and `--depends`
references stay unambiguous.

Setting a closed tag by hand (`update <id> --tag DONE`) prints a warning: the
item stays in the DB, hidden from `list`/rollup but still exported to markdown.
Use `done` or `archive` to close items properly.

### `rollup.py` (cross-project aggregator)

Run from the master hub directory:

```bash
python3 scripts/rollup.py                           # write MASTER_PRIORITIES.md
python3 scripts/rollup.py --html                    # write dashboard.html
python3 scripts/rollup.py --window-days 30          # narrow the deadline window
python3 scripts/rollup.py --json                    # dump to stdout as JSON
```

Items from a project surface in the rollup when any of:
- deadline within `ROLLUP_WINDOW_DAYS` of today,
- title prefixed with `[XP]`,
- `xp_tags` set,
- priority `H`.

Snoozed items never surface, in any tracker including the hub's own.

---

## Feature semantics

### The `[XP]` tag and `xp_tags` field

Two ways to force an item into the cross-project rollup regardless of deadline:

**`[XP]` in the title** (quick, visible):
```bash
python3 scripts/todo.py add --section active --title "[XP] Ship the v1.0 release"
```

**`--xp` flag** (attaches project labels for filtering):
```bash
python3 scripts/todo.py add --section active --title "Review PR" --xp "ProjectA,ProjectB"
python3 scripts/todo.py update <id> --xp "ProjectA"
```

Items with `xp_tags` appear under those labels in the dashboard's project
filter and always surface in the rollup.

### Priority (`--priority H|M|L`)

Sorts after deadline (deadline takes precedence); `[H]`/`[M]`/`[L]` badges
appear in `list` output. High-priority items surface in the hub rollup even
with no near deadline. Priority survives a recurring item's respawn. Clear
with `--priority ""`.

### Snooze / wait-until (`--snooze YYYY-MM-DD`, alias `--wait`)

Hides an item from the default `list` view until the given date, and
suppresses it from the hub rollup regardless of deadline or priority. Use
`list --snoozed` to see only snoozed items; `list --all` includes them.
Clear with `--snooze ""`.

### Recurring deadlines (`--recur RULE`)

Rules: `daily`, `weekly`, `monthly`, `yearly`, or `Nd`/`Nw`/`Nm`/`Ny` (e.g.
`2w`, `3m`). Requires a deadline. Marking a recurring item **done** respawns
it as a new open item at the next occurrence (month-end dates clamp to the
last day of the target month; priority and dependencies carry over).
**`archive` does not respawn** — use it to permanently retire a recurring
task. Clear with `--recur ""`.

### Task dependencies (`--depends ID[,ID]`)

Informational — does not block writes or completion. Unmet dependencies show a
`🔒 blocked by: #X` badge in `list` and the dashboard; the badge clears
automatically when the prerequisite closes. Clear with `--depends ""`.

### Status tags and deadlines from free text

`--status` text is scanned for a leading status keyword (word-boundary match
against `STATUS_KEYWORDS`) and for a `deadline: YYYY-MM-DD` phrase or a bolded
date, which set `status_tag` and `deadline` automatically. Explicit `--tag` /
`--deadline` flags always win. `append` adds a `**YYYY-MM-DD:**`-stamped note
without touching an existing deadline.

---

## The dashboard

`dashboard.html` is a single self-contained file: open it straight from disk
for a read-only view, or run the server (`tracker up`) for live editing.

- **Search** matches title, status, notes, and the cross-project **ref**
  (`ProjectA#12`, `priorities#3`). A query containing `#` searches across all
  views, so a ref resolves even if the item is snoozed or outside the window.
- **Markdown** in status notes renders in the expanded detail row: `**bold**`,
  `` `code` ``, and `[text](https://…)` links. Raw HTML stays escaped.
- **Views**: Surfaced / Overdue / Due Soon / Blocked / Snoozed / All, plus
  project chips (multi-select), status checkboxes, grouping, and sortable
  columns. Filter state persists in `localStorage` and the URL hash.
- **+ Add** and per-row **✎ Edit** modals write through the server to the
  source project DB. Editable: title, owner, deadline, section, status tag,
  XP tags, recurrence, dependencies, priority, snooze. Blank clears a field.
  Without the server, both modals emit a copy-paste CLI command instead.
- **Done / Archive buttons** permanently delete the row from the DB and append
  it to the project's `action_items_archive.md`. Irreversible from the DB
  side — double-check the id.
- **Project is locked in the edit modal.** Moving a task between projects
  means moving between SQLite files: `done` it and re-`add` in the target.
- **Optimistic concurrency**: the page embeds a fingerprint of each task at
  render time; the server rejects a write with a "refresh" prompt if the task
  changed since the page loaded (concurrent CLI edit, another tab).
- **Regen-on-read**: when served, the dashboard regenerates automatically on
  the next page load after any write (API or CLI). Viewing the file directly
  off disk, refresh manually with `python3 scripts/rollup.py --html`.

---

## Security notes

The server (`serve.py`) is localhost-only (`127.0.0.1:8765`) and not intended
to be exposed to a network.

- All SQL uses parameterized statements — no injection risk
- User content is HTML-escaped before rendering (`& < > " '`); the embedded
  JSON escapes `<` so `</script>` / `<!--` in task text cannot break the page
- Project and section inputs are validated against a live whitelist before any
  write; dates are ISO-validated
- Subprocesses use argument lists (`shell=False`) — no shell injection
- **CSRF / DNS-rebinding protection** on write endpoints (`/api/add`,
  `/api/update`, `/api/done`): the `Host` header must match
  `127.0.0.1:PORT`/`localhost:PORT`; `Origin` (when present) must match; and a
  custom `X-Tracker` header is required, which cross-origin requests cannot set
  without a CORS preflight the server never grants
- Writes are serialized in-process via a threading lock; `PRAGMA
  busy_timeout=5000` handles cross-process contention (concurrent CLI edits,
  rollup reads)
- No authentication (relies on localhost isolation)

**Write-locking / multi-machine:**

- The DB uses SQLite's default rollback-journal mode (not WAL). WAL is faster
  for concurrent readers but unsafe on network-synced filesystems (Dropbox,
  NFS). On a local-only filesystem you may enable it with
  `PRAGMA journal_mode=WAL` after `todo.py init`.
- Two machines editing the same `action_items.db` simultaneously can produce a
  cloud-sync conflict copy (binary file; not mergeable). Treat each DB as
  single-writer-at-a-time across machines; `busy_timeout` only protects
  concurrent processes on one machine. This is also why `tracker install`
  (auto-start on every machine) is unsafe on a synced directory.

---

## Schema

One `items` table per `action_items.db`, plus a small `meta` table holding the
monotone id counter:

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
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER);  -- next_sort_id counter
```

Existing `action_items.db` files **upgrade automatically**: `open_db()` adds
any missing column and the `meta` table on every open — no manual migration.

---

## Upgrading sub-projects

When the engine is updated, sub-projects do not auto-update — each holds its
own copy of `scripts/todo.py`:

- **DB auto-migrates** on the next open (columns + `meta` table).
- **Re-copy `todo.py`** from this repo into each sub-project's `scripts/` to
  pick up new flags and fixes.
- The hub's `rollup.py` guards every SELECT with column-existence checks, so a
  new hub safely aggregates a project still running an old engine.

---

## Portability notes

- **macOS / Linux / Windows WSL** all supported. `launch.sh` auto-detects
  `open`/`xdg-open`/`wslview` and uses Python's `os.path.realpath` for symlink
  resolution (no GNU coreutils needed on macOS).
- **Custom port:** set `TRACKER_PORT=NNNN`.
- **Default project roots:** if `PROJECT_ROOTS` is unset, `rollup.py` searches
  the common cloud-sync mounts (`~/Library/CloudStorage/*`, `~/Dropbox`,
  `~/Box`, `~/OneDrive`, `~/Google Drive`) and finally `$HOME`. Set
  `PROJECT_ROOTS` in `tracker_config.py` to be explicit.
- **PATH:** `~/.local/bin` is the recommended home for the `tracker` symlink;
  ensure it is on your `$PATH`.
