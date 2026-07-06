# Using itty-bitty-project-tracker with AI Agents

For AI coding assistants (Claude Code, Cursor, Codex, Gemini, etc.) working in a directory
that uses this tracker. Covers read, write, and the Claude Code `/todo` skill.

---

## Read operations

**Prefer the CLI over reading `action_items.md` directly.** The CLI returns a compact
aligned table; the full markdown export is ~20× larger and contains no additional
information.

```bash
# All open items in this project (default hides snoozed items)
python3 scripts/todo.py list

# Filter by section (slug from SECTION_ORDER in tracker_config.py)
python3 scripts/todo.py list --section <slug>

# Items due before a date
python3 scripts/todo.py list --due-before 2026-08-01

# Text search across title, status, and notes (case-insensitive substring)
python3 scripts/todo.py list --search "keyword"

# Show only currently-snoozed items; --all includes snoozed in normal list
python3 scripts/todo.py list --snoozed
python3 scripts/todo.py list --all

# Full detail on one item (status notes, xp_tags, deadline, priority, recur, depends)
python3 scripts/todo.py show <id>

# Cross-project JSON dump (from the master hub directory)
python3 scripts/rollup.py --json
```

Default `list` output shows badges inline: `[H]`/`[M]`/`[L]` (priority), `🔁` (recurring),
`💤 until <date>` (snoozed, visible only via `--snoozed` or `--all`), `🔒 blocked by: #X`
(unmet dependencies).

To discover valid section slugs for this project, read `scripts/tracker_config.py`
(`SECTION_ORDER` list).

---

## Write operations

```bash
# Add a task (--section defaults to the first slug if omitted)
python3 scripts/todo.py add --title "Task title"
python3 scripts/todo.py add --section active --title "Write intro" --deadline 2026-07-01 --owner "Alice"
python3 scripts/todo.py add --section active --title "[XP] Cross-project item" --xp "ProjectA,ProjectB"

# Priority (H/M/L) — sorts after deadline; [H] surfaces in hub rollup even without a deadline
python3 scripts/todo.py add --title "Urgent thing" --priority H
python3 scripts/todo.py update <id> --priority M
python3 scripts/todo.py update <id> --priority ""          # clear priority

# Snooze / wait-until — hides from default list until date; suppressed from hub rollup
python3 scripts/todo.py add --title "Follow up later" --snooze 2026-08-01
python3 scripts/todo.py update <id> --wait 2026-09-01     # --wait is an alias for --snooze
python3 scripts/todo.py update <id> --snooze ""           # clear snooze

# Recurring deadlines — requires --deadline; Nd/Nw/Nm/Ny or daily/weekly/monthly/yearly
python3 scripts/todo.py add --title "Monthly report" --deadline 2026-07-01 --recur monthly
python3 scripts/todo.py add --title "Sprint review" --deadline 2026-06-30 --recur 2w
python3 scripts/todo.py update <id> --recur ""            # clear recurrence
# On `done`, a recurring item respawns at the next occurrence (archive does not respawn)

# Dependencies — informational; shown as 🔒 until prerequisites close
python3 scripts/todo.py add --title "Deploy" --depends 3,7
python3 scripts/todo.py update <id> --depends 5           # replace list
python3 scripts/todo.py update <id> --depends ""          # clear dependencies

# Update fields
python3 scripts/todo.py update <id> --deadline 2026-07-15
python3 scripts/todo.py update <id> --tag "IN PROGRESS"
python3 scripts/todo.py update <id> --title "New title" --section backlog

# Append a dated status note — non-destructive, always safe for updates
python3 scripts/todo.py append <id> --text "Sent draft to collaborator"

# Mark done and move to archive
python3 scripts/todo.py done <id>
```

**Read before writing:**

- `action_items.md` is auto-generated. Never edit it — writes are silently overwritten on
  the next export.
- **`done` is irreversible from the DB.** The row is permanently deleted and appended to
  `action_items_archive.md`. Recovery requires hand-editing the archive and re-adding.
  Verify the `id` before calling `done`.
- **`done` on a recurring item respawns the next occurrence.** `archive` does not — use
  `archive` to permanently retire a recurring task.
- `--section` must be a valid slug from `SECTION_ORDER` in `scripts/tracker_config.py`.
  Invalid slugs exit non-zero and list valid options.
- `--recur` requires a deadline — `add` exits non-zero if `--deadline` is absent.
- `append` is always safe for status updates — it never overwrites existing notes.
- For cross-project surfacing: prefix the title with `[XP]` or pass `--xp "label"`.
- Passing `--priority ""`, `--snooze ""`, `--recur ""`, or `--depends ""` on `update` clears the field.

---

## Dashboard

When the server is running (`tracker up` or `python3 scripts/serve.py`), the dashboard at
`http://127.0.0.1:8765` auto-regenerates on the next page load after any write — whether
via the API or the CLI. No manual rollup needed when using the served dashboard.

If viewing `dashboard.html` **directly off disk** (without the server), CLI writes do not
auto-refresh it; run:

```bash
python3 scripts/rollup.py --html
```

The dashboard supports inline editing and Done/Archive buttons when the server is running.
Without the server, it is a static read-only view.

`tracker up` runs the server detached (safe to close the terminal); it logs to a
rotating file at `~/Library/Logs/tracker/serve.log` (macOS) or
`${XDG_STATE_HOME:-~/.local/state}/tracker/serve.log` (Linux/WSL) rather than the
terminal. Do not enable `tracker install` (launchd auto-start) on a directory
synced across machines — concurrent servers risk corrupting `action_items.db`.

---

## Master hub vs. sub-project

The **master hub** is the directory with `rollup.py` and a `PROJECTS` list in
`tracker_config.py`. It aggregates items from sub-projects via rollup.

- **Reads across projects:** `python3 scripts/rollup.py --json`
- **Writes always go through the sub-project's own `scripts/todo.py`** — the hub has no
  write path for sub-project items. To add a task to ProjectA, run `todo.py add` from
  inside ProjectA's directory, not from the hub.

---

## Upgrading an existing project to the new engine

When the master tracker repo is updated, sub-project engines do **not** auto-update — each project holds its own copy of `scripts/todo.py`. Two things happen automatically / must be done manually:

- **DB auto-migrates.** `open_db()` runs `_MIGRATIONS` on every open, adding the new columns (`priority`, `wait_until`, `recur`, `depends_on`) to existing `action_items.db` files. No manual SQL needed.
- **Re-copy `todo.py` to get new flags.** Copy `scripts/todo.py` from the master repo into each sub-project's `scripts/` directory to expose `--priority`, `--snooze`/`--wait`, `--recur`, `--depends`, and `list --search`/`--snoozed`.

The hub's `rollup.py` guards every SELECT with `_has_column()` checks, so a hub on the new code can safely aggregate a project still running the old engine without errors.

---

## For Claude Code: the `/todo` skill

If you have the `/todo` skill installed, running `/todo` from any project directory with
`action_items.db` + `scripts/todo.py` present will:

1. Run `python3 scripts/todo.py list --limit 25`
2. Group results by urgency tier (today/imminent → coming up → open/no deadline)
3. Return a compact single-screen table — keeps the conversation context lean

From the **master hub** directory, `/todo` additionally regenerates `MASTER_PRIORITIES.md`
and `dashboard.html` and opens (or refreshes) the server.

Install the skill by copying `skills/todo/SKILL.md` into your Claude Code skills directory
(`~/.claude/skills/todo/SKILL.md`). See the [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code/skills)
for the skill format.

---

## Template: drop into a sub-project's CLAUDE.md

Add this to any project's `CLAUDE.md` to give Claude Code reliable task-tracking context:

```markdown
## Task tracking

This project uses [itty-bitty-project-tracker](https://github.com/gnarayan/itty-bitty-project-tracker).

- **Read:** `python3 scripts/todo.py list` — compact table; default hides snoozed items; prefer over reading action_items.md directly
- **Search:** `python3 scripts/todo.py list --search TERM` — substring across title/status/notes; `--snoozed` shows only snoozed; `--all` includes them
- **Add:** `python3 scripts/todo.py add --title "..."` (--section defaults to first slug)
- **Priority:** `--priority {H,M,L}` on add/update; H surfaces in hub rollup even without deadline; clear with `--priority ""`
- **Snooze:** `--snooze YYYY-MM-DD` (alias `--wait`) on add/update; hides from default list; clear with `--snooze ""`
- **Recur:** `--recur RULE` on add/update (e.g. `weekly`, `2w`, `monthly`); **requires --deadline**; `done` respawns next occurrence, `archive` does not; clear with `--recur ""`
- **Depends:** `--depends ID[,ID]` on add/update; informational; shown as 🔒 until prerequisites close; clear with `--depends ""`
- **Update:** `python3 scripts/todo.py update <id> [--deadline YYYY-MM-DD] [--tag STATUS] [--title ...]`
- **Note:** `python3 scripts/todo.py append <id> --text "status note"` — safe, non-destructive
- **Done:** `python3 scripts/todo.py done <id>` — **irreversible**; confirm id first
- Valid sections: `SECTION_ORDER` in `scripts/tracker_config.py`
- Never edit `action_items.md` directly (auto-generated export).
```
