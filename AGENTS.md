# Using itty-bitty-project-tracker with AI Agents

For AI coding assistants (Claude Code, Cursor, Codex, Gemini, etc.) working in a directory
that uses this tracker. Covers read, write, and the Claude Code `/todo` skill.

---

## Read operations

**Prefer the CLI over reading `action_items.md` directly.** The CLI returns a compact
aligned table; the full markdown export is ~20× larger and contains no additional
information.

```bash
# All open items in this project
python3 scripts/todo.py list

# Filter by section (slug from SECTION_ORDER in tracker_config.py)
python3 scripts/todo.py list --section <slug>

# Items due before a date
python3 scripts/todo.py list --due-before 2026-08-01

# Full detail on one item (status notes, xp_tags, deadline)
python3 scripts/todo.py show <id>

# Cross-project JSON dump (from the master hub directory)
python3 scripts/rollup.py --json
```

To discover valid section slugs for this project, read `scripts/tracker_config.py`
(`SECTION_ORDER` list).

---

## Write operations

```bash
# Add a task (--section defaults to the first slug if omitted)
python3 scripts/todo.py add --title "Task title"
python3 scripts/todo.py add --section active --title "Write intro" --deadline 2026-07-01 --owner "Alice"
python3 scripts/todo.py add --section active --title "[XP] Cross-project item" --xp "ProjectA,ProjectB"

# Update fields
python3 scripts/todo.py update <id> --deadline 2026-07-15
python3 scripts/todo.py update <id> --tag "IN PROGRESS"
python3 scripts/todo.py update <id> --title "New title" --section backlog

# Append a dated status note — non-destructive, always safe for updates
python3 scripts/todo.py append <id> "Sent draft to collaborator"

# Mark done and move to archive
python3 scripts/todo.py done <id>
```

**Read before writing:**

- `action_items.md` is auto-generated. Never edit it — writes are silently overwritten on
  the next export.
- **`done` is irreversible from the DB.** The row is permanently deleted and appended to
  `action_items_archive.md`. Recovery requires hand-editing the archive and re-adding.
  Verify the `id` before calling `done`.
- `--section` must be a valid slug from `SECTION_ORDER` in `scripts/tracker_config.py`.
  Invalid slugs exit non-zero and list valid options.
- `append` is always safe for status updates — it never overwrites existing notes.
- For cross-project surfacing: prefix the title with `[XP]` or pass `--xp "label"`.

---

## Dashboard

When the server is running (`tracker up` or `python3 scripts/serve.py`), the dashboard at
`http://127.0.0.1:8765` auto-regenerates after each API write. After a CLI write, refresh
manually:

```bash
python3 scripts/rollup.py --html
```

The dashboard supports inline editing and Done/Archive buttons when the server is running.
Without the server, it is a static read-only view.

---

## Master hub vs. sub-project

The **master hub** is the directory with `rollup.py` and a `PROJECTS` list in
`tracker_config.py`. It aggregates items from sub-projects via rollup.

- **Reads across projects:** `python3 scripts/rollup.py --json`
- **Writes always go through the sub-project's own `scripts/todo.py`** — the hub has no
  write path for sub-project items. To add a task to ProjectA, run `todo.py add` from
  inside ProjectA's directory, not from the hub.

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

- **Read:** `python3 scripts/todo.py list` — compact table; prefer over reading action_items.md directly
- **Add:** `python3 scripts/todo.py add --title "..."` (--section defaults to first slug)
- **Update:** `python3 scripts/todo.py update <id> [--deadline YYYY-MM-DD] [--tag STATUS] [--title ...]`
- **Note:** `python3 scripts/todo.py append <id> "status note"` — safe, non-destructive
- **Done:** `python3 scripts/todo.py done <id>` — **irreversible**; confirm id first
- Valid sections: `SECTION_ORDER` in `scripts/tracker_config.py`
- Never edit `action_items.md` directly (auto-generated export).
```
