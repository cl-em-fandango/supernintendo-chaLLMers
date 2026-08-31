#!/usr/bin/env bash
#
# set-llm-oom-priority.sh — make the local LLM model server the kernel's
# LAST kill candidate under memory pressure (spec FR-3).
#
# Discovers LLM server processes (llama-server, vllm, ollama) by name and
# writes -1000 to each /proc/<pid>/oom_score_adj. The containerized harness
# deliberately carries oom_score_adj=+500 (see scripts/harness-run), so the
# kill order under pressure is: container subprocesses -> harness container
# -> (never, or last) the LLM server.
#
# PRIVILEGES: writing -1000 to another process's oom_score_adj requires root
# (or CAP_SYS_RESOURCE). Run this via sudo, or install it as a systemd
# oneshot unit running as root (ExecStart=.../set-llm-oom-priority.sh),
# triggered after the LLM server starts and/or on a timer — the script is
# idempotent and safe to re-run.
#
# Per-PID failures (permission denied, process vanished mid-run) are reported
# to stderr as warnings and never abort the script. Exit code is 0 when at
# least one PID was protected or no LLM processes were found; non-zero only
# on unexpected errors (e.g. pgrep unavailable).
#
# Environment:
#   HARNESS_PROC_DIR   proc filesystem root (default /proc; test seam)
#
# Usage: scripts/set-llm-oom-priority.sh
set -u

PROC_ROOT="${HARNESS_PROC_DIR:-/proc}"
OOM_PRIORITY=-1000
LLM_PROCESS_NAMES=(llama-server vllm ollama)

PROTECTED_PIDS=()
SKIPPED_PIDS=()

# ---------------------------------------------------------------------------
# Discovery (FR-3.1): pgrep -f per server name, deduplicated, never matching
# this script or the pgrep invocation itself. pgrep already excludes its own
# PID; $$ (this script) and $PPID (its launcher) are filtered here.
# ---------------------------------------------------------------------------
discover_pids() {
    local name pid seen=" "
    for name in "${LLM_PROCESS_NAMES[@]}"; do
        while IFS= read -r pid; do
            case "$pid" in
                ''|*[!0-9]*) continue ;;
            esac
            if [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ]; then
                continue
            fi
            case "$seen" in *" $pid "*) continue ;; esac
            seen="${seen}${pid} "
            echo "$pid"
        done < <(pgrep -f -- "$name" 2>/dev/null)
    done
}

# ---------------------------------------------------------------------------
# Protection (FR-3.2): write -1000 per PID; failures warn, never abort.
# ---------------------------------------------------------------------------
protect_pid() {
    local pid="$1"
    local target="${PROC_ROOT}/${pid}/oom_score_adj"
    if ! printf '%s\n' "$OOM_PRIORITY" > "$target" 2>/dev/null; then
        echo "Warning: could not write ${OOM_PRIORITY} to ${target} (permission denied or process vanished); skipped." >&2
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Summary (FR-3.3): one line — PIDs protected, and any skipped.
# ---------------------------------------------------------------------------
print_summary() {
    local line="==> LLM OOM priority: ${#PROTECTED_PIDS[@]} PID(s) protected"
    if [ "${#PROTECTED_PIDS[@]}" -gt 0 ]; then
        line="${line} [${PROTECTED_PIDS[*]}]"
    fi
    if [ "${#SKIPPED_PIDS[@]}" -gt 0 ]; then
        line="${line}; ${#SKIPPED_PIDS[@]} skipped [${SKIPPED_PIDS[*]}]"
    fi
    echo "$line"
}

main() {
    if ! command -v pgrep >/dev/null 2>&1; then
        echo "Error: pgrep not found in PATH; cannot discover LLM server PIDs." >&2
        exit 1
    fi
    local pid
    for pid in $(discover_pids); do
        if protect_pid "$pid"; then
            PROTECTED_PIDS+=("$pid")
        else
            SKIPPED_PIDS+=("$pid")
        fi
    done
    print_summary
}

main "$@"
