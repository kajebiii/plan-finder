#!/bin/bash
# plan-finder daemon wrapper
# Usage: ./plan-finder-daemon.sh start [--at HH:MM] [-- plan-finder args...]
# Usage: ./plan-finder-daemon.sh stop
# Usage: ./plan-finder-daemon.sh status

PID_FILE="$HOME/.plan-finder-daemon.pid"
LOG_FILE="$HOME/.plan-finder-daemon.log"
ARGS_FILE="$HOME/.plan-finder-daemon.args"
PRE_HOOK_FILE="$HOME/.plan-finder-daemon.pre-hook"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

run_daemon() {
    echo $$ > "$PID_FILE"

    TARGET_TIME=""
    if [ -f "$HOME/.plan-finder-daemon.target-time" ]; then
        TARGET_TIME=$(cat "$HOME/.plan-finder-daemon.target-time")
    fi

    CWD=""
    if [ -f "$HOME/.plan-finder-daemon.cwd" ]; then
        CWD=$(cat "$HOME/.plan-finder-daemon.cwd")
    fi

    # Persistent config files (kept across runs so launchd KeepAlive can re-read on restart)
    PF_ARGS=()
    if [ -f "$ARGS_FILE" ]; then
        while IFS= read -r line; do
            PF_ARGS+=("$line")
        done < "$ARGS_FILE"
    fi

    log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"; }

    export PATH="$HOME/.local/bin:/etc/profiles/per-user/$USER/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

    while true; do
        # Wait until target time if set
        if [ -n "$TARGET_TIME" ]; then
            log "Waiting until $TARGET_TIME to start..."
            # Calculate next occurrence of TARGET_TIME as epoch
            TARGET_EPOCH=$(date -j -f "%Y-%m-%d %H:%M:%S" "$(date '+%Y-%m-%d') $TARGET_TIME:00" +%s 2>/dev/null)
            CURRENT_EPOCH=$(date +%s)
            # If target time already passed today, aim for tomorrow
            if [ "$CURRENT_EPOCH" -ge "$TARGET_EPOCH" ]; then
                TARGET_EPOCH=$((TARGET_EPOCH + 86400))
                log "Target time passed today, next run at $(date -r "$TARGET_EPOCH" '+%Y-%m-%d %H:%M')"
            fi
            # Epoch-based wait: survives macOS sleep (date +%s always returns real time)
            while [ "$(date +%s)" -lt "$TARGET_EPOCH" ]; do
                sleep 30
            done

            # Extract --stop-at from PF_ARGS to skip if we woke up too late
            STOP_AT=""
            for i in "${!PF_ARGS[@]}"; do
                if [ "${PF_ARGS[$i]}" = "--stop-at" ]; then
                    STOP_AT="${PF_ARGS[$((i+1))]}"
                    break
                fi
            done
            if [ -n "$STOP_AT" ]; then
                STOP_EPOCH=$(date -j -f "%Y-%m-%d %H:%M:%S" "$(date -r "$TARGET_EPOCH" '+%Y-%m-%d') $STOP_AT:00" +%s 2>/dev/null)
                # If stop time is before start time, it means stop is same day (e.g. start 03:00, stop 07:30)
                if [ "$(date +%s)" -ge "$STOP_EPOCH" ]; then
                    log "Woke up after stop time ($STOP_AT). Skipping today's run."
                    continue
                fi
            fi
        fi

        # Pre-run hook (optional). Lives at $PRE_HOOK_FILE so the daemon stays
        # project-agnostic — put repo pulls, secret refresh, etc. in that file.
        # Failures are logged but never abort the run.
        if [ -f "$PRE_HOOK_FILE" ]; then
            log "Running pre-hook: $PRE_HOOK_FILE"
            bash "$PRE_HOOK_FILE" >> "$LOG_FILE" 2>&1 \
                || log "pre-hook failed (continuing anyway)"
        fi

        log "Starting plan-finder with args: ${PF_ARGS[*]}"
        log "CWD: ${CWD:-$HOME}"

        cd "${CWD:-$HOME}"
        uv run --project "$SCRIPT_DIR" plan-finder "${PF_ARGS[@]}" >> "$LOG_FILE" 2>&1
        EXIT_CODE=$?

        log "plan-finder finished with exit code $EXIT_CODE"

        # If no target time, run once and exit (backwards compat)
        if [ -z "$TARGET_TIME" ]; then
            break
        fi

        # Sleep past the current minute to avoid re-triggering immediately
        sleep 90
    done

    rm -f "$PID_FILE"
}

start_daemon() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Already running (PID $(cat "$PID_FILE")). Stop first: $0 stop"
        exit 1
    fi

    # Parse --at TIME
    TARGET_TIME=""
    shift  # remove 'start'
    if [ "$1" = "--at" ]; then
        TARGET_TIME="$2"
        shift 2
    fi

    # Skip '--' separator if present
    [ "$1" = "--" ] && shift

    # Save args to file (one per line) to avoid quoting issues
    printf '%s\n' "$@" > "$ARGS_FILE"

    # Save target time and cwd
    [ -n "$TARGET_TIME" ] && echo "$TARGET_TIME" > "$HOME/.plan-finder-daemon.target-time"
    pwd > "$HOME/.plan-finder-daemon.cwd"

    # Run in background
    nohup "$0" _run > /dev/null 2>&1 &

    sleep 1
    if [ -f "$PID_FILE" ]; then
        echo "Daemon started (PID $(cat "$PID_FILE"))"
        echo "Log: $LOG_FILE"
        [ -n "$TARGET_TIME" ] && echo "Will run at: $TARGET_TIME"
    else
        echo "Failed to start daemon"
    fi
}

stop_daemon() {
    if [ ! -f "$PID_FILE" ]; then
        echo "Not running"
        exit 0
    fi
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Stopped (PID $PID)"
    else
        echo "Process $PID not found (already stopped)"
    fi
    rm -f "$PID_FILE"
}

status_daemon() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Running (PID $(cat "$PID_FILE"))"
        echo "Recent log:"
        tail -5 "$LOG_FILE" 2>/dev/null
    else
        echo "Not running"
    fi
}

case "$1" in
    start)  start_daemon "$@" ;;
    stop)   stop_daemon ;;
    status) status_daemon ;;
    _run)   run_daemon ;;
    *)
        echo "Usage: $0 {start|stop|status} [--at HH:MM] [-- plan-finder-args...]"
        echo ""
        echo "Examples:"
        echo "  $0 start -- --auto --prompt 'find improvements' --max 50"
        echo "  $0 start --at 02:59 -- --auto --prompt 'find improvements' --max 50"
        echo "  $0 stop"
        echo "  $0 status"
        ;;
esac
