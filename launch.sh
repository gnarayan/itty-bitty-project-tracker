#!/usr/bin/env bash
# tracker [up|down|restart|add|list|done <id>|init-project <label> <path>]
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

is_up() { curl -sf -m 2 "$URL/api/ping" > /dev/null; }

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
        python3 "$DIR/scripts/serve.py" --port "$PORT" &
        for i in 1 2 3 4 5; do
            sleep 1
            is_up && break
        done
    fi
    opener "$URL"
}

do_down() {
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
do_add()  { python3 "$DIR/scripts/todo.py" add  "$@"; }
do_list() { python3 "$DIR/scripts/todo.py" list "$@"; }
do_done() { python3 "$DIR/scripts/todo.py" done "$@"; }

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

case "${1:-up}" in
    up)           do_up ;;
    down)         do_down ;;
    restart)      do_down; sleep 1; do_up ;;
    add)          shift; do_add  "$@" ;;
    list)         shift; do_list "$@" ;;
    done)         shift; do_done "$@" ;;
    init-project) do_init_project "${2:-}" "${3:-}" ;;
    *)
        echo "Usage: tracker [up|down|restart|add [opts]|list [opts]|done <id>|init-project <label> <path>]" >&2
        exit 1
        ;;
esac
