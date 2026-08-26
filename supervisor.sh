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
FAIL_LIMIT="${FAIL_LIMIT:-3}"   # consecutive harness launch failures before auto-revert
CONTINUE="${CONTINUE:-1}"       # 1 = pass --continue to run-task-loop (resume in-flight tasks)
FAILCOUNT=0

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

  # --- circuit breaker: is the harness even launchable? ---
  if ! ( cd "$HARNESS_DIR" && python3 harness.py status ) >> "$LOG" 2>&1; then
    FAILCOUNT=$((FAILCOUNT+1))
    log "  ⚠ harness failed to launch ($FAILCOUNT/$FAIL_LIMIT)"
    if [[ "$FAILCOUNT" -ge "$FAIL_LIMIT" ]]; then
      log "  ⛔ CIRCUIT BREAKER: reverting trunk to pi/last-good"
      ( cd "$HARNESS_DIR" && git reset --hard pi/last-good ) >> "$LOG" 2>&1 \
        && log "  reverted to $(cd "$HARNESS_DIR" && git rev-parse --short pi/last-good)" \
        || log "  ⚠ revert failed — manual intervention needed"
      FAILCOUNT=0
      sleep "$SLEEP_S"
      continue
    fi
    sleep "$SLEEP_S"
    continue
  fi
  FAILCOUNT=0   # harness launched fine; reset the failure counter

  pending=$(ls "$WORK_DIR/queue/pending"/*.md 2>/dev/null | wc -l)
  inflight=$(find "$WORK_DIR/queue/active" -mindepth 2 -maxdepth 2 -name task.json 2>/dev/null | wc -l)
  log "── cycle $cycle: pending=$pending, in-flight=$inflight ──"

  if [[ "$pending" -gt 0 || "$inflight" -gt 0 ]]; then
    if [[ "$CONTINUE" == "1" ]]; then
      log "processing pending tasks (resuming in-flight)"
      ( cd "$HARNESS_DIR" && python3 harness.py run-task-loop --continue ) >> "$LOG" 2>&1 \
        || log "  run-task-loop exited rc=$?"
    else
      log "processing pending tasks"
      ( cd "$HARNESS_DIR" && python3 harness.py run-task-loop ) >> "$LOG" 2>&1 \
        || log "  run-task-loop exited rc=$?"
    fi
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
