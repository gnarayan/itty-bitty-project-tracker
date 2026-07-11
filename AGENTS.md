# Using itty-bitty-project-tracker with AI Agents

For AI coding assistants (Claude Code, Cursor, Codex, Gemini, …) working in a
directory that uses this tracker. Full flag reference: [REFERENCE.md](REFERENCE.md).

---

## Invariants — read before writing

1. **Never edit `action_items.md`.** It is an auto-generated export; edits are
   silently overwritten on the next `export`, `done`, or `archive`.
2. **`done` is irreversible from the DB.** The row is permanently deleted and
   appended to `action_items_archive.md`. Verify the id (`show <id>`) first.
3. **Recurring items: `done` respawns the next occurrence; `archive` retires
   the task permanently.** Choose deliberately.
4. **All reads and writes go through `python3 scripts/todo.py`** — never edit
   the DB with ad-hoc sqlite. The CLI parameterizes everything, so quotes,
   URLs, and markdown in text are never an escaping problem.
5. **`--section` must be a slug from `SECTION_ORDER`** in
   `scripts/tracker_config.py` (read that file to discover valid slugs).
   Invalid slugs exit non-zero and list the valid options.
6. **Hub vs. sub-project:** the hub (the directory with `rollup.py` and a
   `PROJECTS` list) reads across projects (`rollup.py --json`) but has **no
   write path for sub-project items** — to modify ProjectA's items, run
   `todo.py` from inside ProjectA's directory.
7. **`append` is the always-safe status update** — it adds a dated note and
   never overwrites existing text. Prefer it over `update --status` unless you
   mean to replace the whole status.
8. **Prefer the CLI over reading `action_items.md`**: `list` output is a
   compact table ~20× smaller with the same information.
9. **Ids are short hashes** (e.g. `a3f8`), never reused. Old numeric ids from
   before the hash migration still resolve everywhere (`legacy_id` fallback) —
   but always cite the hash id from `list` output in new notes and `--depends`.

---

## Command crib

```bash
# Orient (start of session)
python3 scripts/todo.py prime                      # counts + overdue/ready/blocked + conventions, agent-ready
python3 scripts/todo.py ready [--json]             # open items with no active blockers — "what can I work on now"

# Read
python3 scripts/todo.py list                       # open items (snoozed hidden); badges: [H/M/L], 🔁, 💤, 🔒
python3 scripts/todo.py list --section <slug> | --due-before YYYY-MM-DD | --search TERM
python3 scripts/todo.py list --snoozed | --all | --standing | --json
python3 scripts/todo.py show <id>                  # full record incl. status notes
python3 scripts/rollup.py --json                   # cross-project dump (hub dir only)

# Write
python3 scripts/todo.py claim <id> [--by NAME]     # atomic: sets IN PROGRESS, fails if already claimed; release: update <id> --tag OPEN
python3 scripts/todo.py add --title "..." [--section slug] [--deadline YYYY-MM-DD] [--owner X]
python3 scripts/todo.py add --title "[XP] cross-project item"      # or --xp "LabelA,LabelB"
python3 scripts/todo.py update <id> [--deadline YYYY-MM-DD] [--tag STATUS] [--title ...] [--section slug]
python3 scripts/todo.py update <id> --priority H|M|L | --snooze YYYY-MM-DD | --recur 2w | --depends 3,7
python3 scripts/todo.py update <id> --status-file -   # multi-line status via stdin
python3 scripts/todo.py append <id> --text "dated status note"
python3 scripts/todo.py done <id>                  # close (see invariants 2–3)
python3 scripts/todo.py archive <id>
```

Field-clearing on `update`: pass `""` to `--deadline`, `--priority`,
`--snooze`, `--recur`, `--depends`, or `--xp`. `--recur` requires a deadline.
`--wait` is an alias for `--snooze`.

---

## Dashboard

When the server is running (`tracker up`), `http://127.0.0.1:8765`
auto-regenerates on the next page load after any write — API or CLI; no manual
rollup needed. Off-disk `dashboard.html` (no server) is a static view; refresh
it with `python3 scripts/rollup.py --html` after CLI writes. Do not enable
`tracker install` on a directory synced across machines
([why](REFERENCE.md#security-notes)).

---

## Template: drop into a project's CLAUDE.md

```markdown
## Task tracking

This project uses [itty-bitty-project-tracker](https://github.com/gnarayan/itty-bitty-project-tracker).

- **Orient:** `python3 scripts/todo.py prime` (counts + overdue/ready/blocked + conventions); `ready` for unblocked items
- **Read:** `python3 scripts/todo.py list` (compact table; prefer over action_items.md); `show <id>` for detail
- **Search:** `list --search TERM`; `--snoozed`/`--all`/`--standing` widen the view
- **Claim:** `claim <id> [--by NAME]` before starting work — atomic, fails if another session already claimed it
- **Add:** `python3 scripts/todo.py add --title "..."` (--section defaults to first slug in scripts/tracker_config.py)
- **Update:** `update <id> [--deadline YYYY-MM-DD] [--tag STATUS] [--priority H|M|L] [--snooze DATE] [--recur RULE] [--depends IDs]` — pass "" to clear a field; --recur requires a deadline
- **Note:** `append <id> --text "..."` — dated, non-destructive; the safe default for status updates
- **Close:** `done <id>` — irreversible, respawns recurring items; `archive <id>` retires them
- Never edit `action_items.md` (auto-generated); never bypass the CLI with raw sqlite
```

A `/todo`-style skill for Claude Code can simply wrap `list` (run it, group by
urgency, emit one compact table) — the CLI output is already designed to keep
conversation context lean.
