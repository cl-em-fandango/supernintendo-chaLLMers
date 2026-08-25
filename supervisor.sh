#!/usr/bin/env bash
# Supervisor: keep the harness running in bounded cycles until stopped.
#
# Each cycle:
#   1. if there are pending tasks -> run the pipeline on them
#   2. if the queue is empty -> autonomous mode generates new tasks
#   3. sleep, then repeat
#
# Stop it with:  supervisor.sh stop     (or kill the pid in supervisor.pid)
#
# Safety valves:
#   - each cycle is bounded by the pipeline's own iteration caps
#   - MAX_CYCLES caps total cycles (default: unlimited, set to stop)
#   - a STOP file (work/STOP) halts the loop on the next check
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="/home/donald/work"
LOG="$WORK_DIR/logs/supervisor.log"
PIDFILE="$WORK_DIR/logs/supervisor.pid"
STOPFILE="$WORK_DIR/STOP"
SLEEP_S="${SLEEP_S:-60}"
MAX_CYCLES="${MAX_CYCLES:-0}"   # 0 = unlimited

mkdir -p "$WORK_DIR/logs"
log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }

case "${1:-loop}" in
  stop)
    if [[ -f "$PIDFILE" ]]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null && log "supervisor stopped" || log "no supervisor running"
      rm -f "$PIDFILE"
    else
      log "no supervisor pidfile"
    fi
    exit 0
    ;;
  status)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "supervisor running (pid $(cat "$PIDFILE"))"
    else
      echo "supervisor not running"
    fi
    exit 0
    ;;
esac

# --- main loop ---
echo $$ > "$PIDFILE"
log "supervisor started (pid $$, sleep=${SLEEP_S}s, max_cycles=${MAX_CYCLES})"

cycle=0
while true; do
  if [[ -f "$STOPFILE" ]]; then
    log "STOP file present; halting"
    rm -f "$STOPFILE"
    break
  fi
  cycle=$((cycle+1))
  if [[ "$MAX_CYCLES" -gt 0 && $cycle -gt "$MAX_CYCLES" ]]; then
    log "reached MAX_CYCLES=$MAX_CYCLES; halting"
    break
  fi

  pending=$(ls "$WORK_DIR/queue/pending"/*.md 2>/dev/null | wc -l)
  log "── cycle $cycle: pending=$pending ──"

  if [[ "$pending" -gt 0 ]]; then
    log "processing pending tasks"
    ( cd "$HARNESS_DIR" && python3 harness.py run-task-loop ) >> "$LOG" 2>&1 \
      || log "  run-task-loop exited rc=$?"
  else
    log "queue empty -> autonomous generation"
    ( cd "$HARNESS_DIR" && python3 harness.py autonomous ) >> "$LOG" 2>&1 \
      || log "  autonomous exited rc=$?"
  fi

  # quick stats snapshot
  ( cd "$HARNESS_DIR" && python3 harness.py report ) >> "$LOG" 2>&1

  log "sleeping ${SLEEP_S}s"
  sleep "$SLEEP_S"
done

rm -f "$PIDFILE"
log "supervisor exited"
