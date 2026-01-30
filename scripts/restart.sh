#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="ccmux"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="/data/code/ccmux"
MAX_WAIT=10  # seconds to wait for process to exit

# Check if tmux session and window exist
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Error: tmux session '$TMUX_SESSION' does not exist"
    exit 1
fi

if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Error: window '$TMUX_WINDOW' not found in session '$TMUX_SESSION'"
    exit 1
fi

# Get the pane PID and check if uv run ccmux is running
PANE_PID=$(tmux list-panes -t "$TARGET" -F '#{pane_pid}')

is_ccmux_running() {
    pstree -a "$PANE_PID" 2>/dev/null | grep -q 'uv.*run ccmux\|ccmux.*\.venv/bin/ccmux'
}

# Stop existing process if running
if is_ccmux_running; then
    echo "Found running ccmux process, sending Ctrl-C..."
    tmux send-keys -t "$TARGET" C-c

    # Wait for process to exit
    waited=0
    while is_ccmux_running && [ "$waited" -lt "$MAX_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting for process to exit... (${waited}s/${MAX_WAIT}s)"
    done

    if is_ccmux_running; then
        echo "Process did not exit after ${MAX_WAIT}s, sending SIGTERM..."
        # Kill the uv process directly
        UV_PID=$(pstree -ap "$PANE_PID" 2>/dev/null | grep -oP 'uv,\K\d+' | head -1)
        if [ -n "$UV_PID" ]; then
            kill "$UV_PID" 2>/dev/null || true
            sleep 2
        fi
        if is_ccmux_running; then
            echo "Process still running, sending SIGKILL..."
            kill -9 "$UV_PID" 2>/dev/null || true
            sleep 1
        fi
    fi

    echo "Process stopped."
else
    echo "No ccmux process running in $TARGET"
fi

# Brief pause to let the shell settle
sleep 1

# Start ccmux
echo "Starting ccmux in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && uv run ccmux" Enter

# Verify startup and show logs
sleep 3
if is_ccmux_running; then
    echo "ccmux restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccmux may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
