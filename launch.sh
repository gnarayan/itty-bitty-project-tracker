#!/usr/bin/env bash
# tracker [up|down|restart|install|uninstall|add|list|ready|done <id>|init-project <label> <path>]
#
# Manages the priorities dashboard server and provides short aliases for the
# most-used todo.py commands.
#
# PORT: override the default server port with the TRACKER_PORT env variable.
set -euo pipefail

# Resolve this script's real location even when invoked through a symlink.
# Uses Python's os.path.realpath — works on macOS, Linux, and Windows WSL
# without needing GNU coreutils (readlink -f is GNU-only; not on stock macOS).
_self="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "${BASH_SOURCE[0]}")"
DIR="$(cd "$(dirname "$_self")" && pwd -P)"

PORT="${TRACKER_PORT:-8765}"
URL="http://127.0.0.1:$PORT"

# launchd LaunchAgent identity (macOS only; see do_install/do_uninstall).
LABEL="com.ittybitty.tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

is_up() { curl -sf -m 2 "$URL/api/ping" > /dev/null; }

# Platform-appropriate directory for the rotating server log.
log_dir() {
    if [ "$(uname)" = "Darwin" ]; then
        echo "$HOME/Library/Logs/tracker"
    else
        echo "${XDG_STATE_HOME:-$HOME/.local/state}/tracker"
    fi
}

# True when a launchd agent for the tracker is currently loaded (macOS only).
launchd_loaded() {
    [ "$(uname)" = "Darwin" ] && \
        launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
}

# Open URL in the default browser (macOS / Linux / WSL).
opener() {
    if   command -v open     >/dev/null 2>&1; then open     "$1"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1"
    elif command -v wslview  >/dev/null 2>&1; then wslview  "$1"
    else echo "Browser not opened (no 'open'/'xdg-open'/'wslview' found). Visit: $1" >&2
    fi
}

do_up() {
    if is_up; then
        echo "Already running at $URL"
    else
        local logdir
        logdir="$(log_dir)"
        mkdir -p "$logdir"
        # nohup + disown so the server survives closing the terminal, and
        # redirect output to the rotating log so nothing spews to the terminal.
        # serve.py owns the rotating log via --log-file; the >> redirect is a
        # safety net for anything printed before logging is configured.
        nohup python3 "$DIR/scripts/serve.py" --port "$PORT" --no-browser \
              --log-file "$logdir/serve.log" >> "$logdir/serve.log" 2>&1 &
        disown
        echo "Started in the background. Logs: $logdir/serve.log"
        for i in 1 2 3 4 5; do
            sleep 1
            is_up && break
        done
    fi
    opener "$URL"
}

do_down() {
    if launchd_loaded; then
        echo "Managed by launchd. A plain stop would just be restarted." >&2
        echo "To stop now without removing the agent:" >&2
        echo "    launchctl bootout gui/$(id -u)/$LABEL" >&2
        echo "To remove the agent entirely (no restart at next login):" >&2
        echo "    tracker uninstall" >&2
        return 1
    fi
    local pids
    pids=$(lsof -ti ":$PORT" 2>/dev/null || true)
    if [ -z "$pids" ]; then
        echo "Not running."
    else
        kill $pids && echo "Stopped."
    fi
}

# Short aliases — pass all remaining arguments through to todo.py.
# --section defaults to the first configured slug when omitted (see todo.py).
do_add()   { python3 "$DIR/scripts/todo.py" add   "$@"; }
do_list()  { python3 "$DIR/scripts/todo.py" list  "$@"; }
do_ready() { python3 "$DIR/scripts/todo.py" ready "$@"; }
do_done()  { python3 "$DIR/scripts/todo.py" done  "$@"; }

do_init_project() {
    local label="$1"
    local proj_path="$2"

    if [ -z "$label" ] || [ -z "$proj_path" ]; then
        echo "Usage: tracker init-project <label> <path>" >&2
        exit 1
    fi

    # Expand ~ in path
    proj_path="${proj_path/#\~/$HOME}"

    mkdir -p "$proj_path/scripts"
    cp "$DIR/scripts/todo.py" "$proj_path/scripts/todo.py"

    if [ ! -f "$proj_path/scripts/tracker_config.py" ]; then
        cp "$DIR/tracker_config.example.py" "$proj_path/scripts/tracker_config.py"
        python3 - "$proj_path/scripts/tracker_config.py" "$label" << 'PYEOF'
import re, sys
path, label = sys.argv[1], sys.argv[2]
txt = open(path).read()
txt = re.sub('PROJECT_TITLE\\s*=\\s*"My Priorities"', f'PROJECT_TITLE = "{label}"', txt)
open(path, 'w').write(txt)
PYEOF
    fi

    (cd "$proj_path" && python3 scripts/todo.py init)

    echo ""
    echo "Project '$label' ready at: $proj_path"
    echo ""
    echo "Next: add it to the master scripts/tracker_config.py PROJECTS list:"
    echo "    (\"$label\", \"$proj_path\"),"
    echo ""
    echo "Then regenerate the dashboard:"
    echo "    python3 scripts/rollup.py --html"
}

# Register a launchd LaunchAgent so the server starts at login and is restarted
# if it ever crashes (macOS only). On other platforms, print pointers instead.
#
# WARNING: do NOT install this when the tracker directory is synced across
# machines (Dropbox / iCloud / Google Drive). A server auto-started on every
# machine means concurrent writers to action_items.db and the .md files, which
# produces "conflicted copy" files and risks database corruption. Multi-machine
# users should rely on on-demand `tracker up` on whichever machine is in use.
do_install() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "tracker install uses macOS launchd and is macOS-only." >&2
        echo "On Linux, run as a systemd --user service, e.g.:" >&2
        echo "    systemd-run --user --unit=tracker \\" >&2
        echo "        python3 $DIR/scripts/serve.py --port $PORT --no-browser \\" >&2
        echo "        --log-file \"\$(log_dir)/serve.log\"" >&2
        echo "or add an '@reboot' crontab entry calling 'tracker up'." >&2
        echo "Everywhere: 'tracker up' already backgrounds safely." >&2
        exit 1
    fi

    local py logdir
    py="$(python3 -c 'import sys; print(sys.executable)')"
    logdir="$(log_dir)"
    mkdir -p "$logdir" "$(dirname "$PLIST")"

    # Generate the plist with stdlib plistlib (safer than templating XML) and
    # the absolute interpreter path (launchd's minimal PATH would not find a
    # conda/pyenv python3 on its own).
    python3 - "$PLIST" "$LABEL" "$py" "$DIR/scripts/serve.py" "$PORT" "$logdir" "$DIR" <<'PYEOF'
import os, plistlib, sys
plist_path, label, py, script, port, logdir, workdir = sys.argv[1:8]
d = {
    "Label": label,
    "ProgramArguments": [py, script, "--port", port, "--no-browser",
                         "--log-file", os.path.join(logdir, "serve.log")],
    "WorkingDirectory": workdir,
    "EnvironmentVariables": {
        "TRACKER_PORT": port,
        "PATH": os.path.dirname(py) + ":/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "RunAtLoad": True,
    # Restart on crash / non-zero exit, but a clean stop (bootout) stays down.
    "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "StandardOutPath": os.path.join(logdir, "launchd.out.log"),
    "StandardErrorPath": os.path.join(logdir, "launchd.err.log"),
}
with open(plist_path, "wb") as f:
    plistlib.dump(d, f)
PYEOF

    # bootout first so re-running install reloads cleanly; ignore "not loaded".
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed and loaded $LABEL."
    echo "Starts at login, restarts on crash. Logs: $logdir/serve.log"
    echo "Stop/remove with: tracker uninstall"
}

do_uninstall() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "tracker uninstall is macOS-only." >&2
        exit 1
    fi
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" && echo "Uninstalled $LABEL."
}

case "${1:-up}" in
    up)           do_up ;;
    down)         do_down ;;
    restart)      do_down; sleep 1; do_up ;;
    install)      do_install ;;
    uninstall)    do_uninstall ;;
    add)          shift; do_add   "$@" ;;
    list)         shift; do_list  "$@" ;;
    ready)        shift; do_ready "$@" ;;
    done)         shift; do_done  "$@" ;;
    init-project) do_init_project "${2:-}" "${3:-}" ;;
    *)
        echo "Usage: tracker [up|down|restart|install|uninstall|add [opts]|list [opts]|ready [opts]|done <id>|init-project <label> <path>]" >&2
        exit 1
        ;;
esac
