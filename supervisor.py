#!/usr/bin/env python3
"""Supervisor: keep the harness running in bounded cycles until stopped.

Pure-Python replacement for the old supervisor.sh. Hardening over the bash
version:
  - Single instance enforced by a PID lockfile with a liveness check
    (no more two supervisors running at once).
  - One cycle = status probe -> decide -> one child -> sleep. The decision
    (harness.workflow.cycle.decide_cycle_action) reads pending/, active/ and
    claimed/, and the child's argv comes from cycle.command_for_action (its log
    label from cycle.subcommand_for_action) rather than from a literal in the
    loop: "run-task-loop" with "--continue" to resume what is in active/ and
    then work the queue one claim at a time, or "autonomous" to generate when
    there is nothing left to work. The old loop
    counted pending/ alone, so a task already in active/ was never resumed.
  - A claimed-only queue (pending/ and active/ empty, claimed/ not) is
    `CycleAction.BLOCKED`, not work (T44): it spawns no child at all, logs one
    operator-action line naming `harness.py requeue-claims --dry-run` and the
    claim count, and idles through the same backoff. Spawning
    "run-task-loop --continue" for it could only ever be a no-op child, because
    that command consumes active/ and pending/ and stale reclaim is opt-in (D4).
  - No-progress backoff: the same three counts are read again after the child
    exits, and a cycle that left them exactly as it found them accomplished
    nothing, so the sleep doubles (SLEEP_S, 2x, 4x, ... up to MAX_SLEEP_S,
    default 900s) and the streak is logged. Any state change resets the sleep
    to SLEEP_S. A wedged task or an unreachable model endpoint therefore stops
    costing a full probe-and-spawn every SLEEP_S forever. The sleep is still
    the interruptible _sleep(), so a stop is honoured mid-backoff.
  - Tracks the child pi process and kills the whole process tree on stop,
    so a hung session can't orphan a model.
  - Circuit breaker: if the harness fails to launch N times in a row, revert
    trunk to pi/last-good and continue. The revert goes through
    external.git_cli, so a worktree with uncommitted changes makes it refuse:
    the refusal is logged and the loop keeps running (never a raw
    `git reset --hard` from here).
  - Graceful stop via `supervisor.py stop` (SIGTERM) or a STOP file.

Log handling:
  supervisor.log is capped at MAX_LOG_BYTES (default 5000000, override with the
  SUPERVISOR_MAX_LOG_BYTES env var). When the next record would push the file
  past the cap, the log is renamed to supervisor.log.1 (replacing whatever
  generation was there before) and appending continues into a fresh
  supervisor.log. Exactly one generation is kept — no date-suffixed pile-up —
  so supervisor.log + supervisor.log.1 are bounded at 2 * MAX_LOG_BYTES.
  A rotation that fails is warned about once and never aborts the loop.

Child output:
  A supervised child's stdout and stderr go to a shared file handle on
  <WORK_DIR>/logs/children/<UTC ts>-<label>.log (one fd keeps their relative
  order), delimited by "=== spawn ... ===" / "=== exited rc=N ===" banner
  lines, so verdicts, heartbeats and tracebacks survive a park or a kill.
  <label> names the subcommand that ran — "status", "run-task-loop",
  "autonomous" — so a file is findable by what the child was doing.
  The directory is capped at MAX_CHILD_LOGS files (default 50, override with
  the SUPERVISOR_MAX_CHILD_LOGS env var): the oldest are deleted before each
  spawn, counting the file about to be created. Every spawn logs the child
  log path to supervisor.log so a human can tail the right file.

Usage:
  supervisor.py run          # run the loop (foreground)
  supervisor.py start        # daemonize and run
  supervisor.py stop         # stop a running supervisor
  supervisor.py status       # is it running?
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.core.config import load            # noqa: E402
from harness.core.providers import TaskProvider, create_provider  # noqa: E402
from harness.workflow.continue_fresh import in_flight_task_dirs  # noqa: E402
from harness.workflow.cycle import (CycleAction, QueueCounts,  # noqa: E402
                                    backoff_seconds, command_for_action,
                                    cycle_summary, decide_cycle_action,
                                    subcommand_for_action)
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402
from external.git_cli import revert_to_last_good  # noqa: E402

HARNESS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = HARNESS_DIR / "config.json"
WORK_DIR = Path(load(CONFIG_PATH).work_dir)
TRUNK = load(CONFIG_PATH).trunk_branch   # the branch the breaker rolls back
LOG = WORK_DIR / "logs" / "supervisor.log"
PIDFILE = WORK_DIR / "logs" / "supervisor.pid"
STOPFILE = WORK_DIR / "STOP"

SLEEP_S = int(os.environ.get("SLEEP_S", "60"))
MAX_SLEEP_S = int(os.environ.get("SUPERVISOR_MAX_SLEEP_S", "900"))  # backoff cap
MAX_CYCLES = int(os.environ.get("MAX_CYCLES", "0"))   # 0 = unlimited
FAIL_LIMIT = int(os.environ.get("FAIL_LIMIT", "3"))

LOG_ENCODING = "utf-8"
MAX_LOG_BYTES = int(os.environ.get("SUPERVISOR_MAX_LOG_BYTES", "5_000_000"))
MAX_CHILD_LOGS = int(os.environ.get("SUPERVISOR_MAX_CHILD_LOGS", "50"))

_rotation_warned = False   # warn once per process, not once per failed write


def _log_size() -> int:
    """Size of the current log in bytes; 0 when it does not exist yet."""
    try:
        return LOG.stat().st_size
    except OSError:
        return 0


def _rotate_log() -> None:
    """Move LOG aside as LOG.1, replacing the previous generation.

    Never raises: if the rename fails we keep appending to the same file and
    say so once, because a wedged or full disk must not kill the supervisor.
    """
    global _rotation_warned
    try:
        os.replace(LOG, LOG.with_name(LOG.name + ".1"))
    except OSError as exc:
        if not _rotation_warned:
            _rotation_warned = True
            print(f"supervisor: WARNING log rotation failed ({exc}); "
                  "appending un-rotated (warning shown once)",
                  file=sys.stderr, flush=True)


def log(msg: str) -> None:
    """Append one record to supervisor.log, rotating first if it would overflow.

    The record is formatted and encoded before the size check, so the bytes we
    measure are exactly the bytes we write (multibyte characters count as their
    UTF-8 length).
    """
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    record = (line + "\n").encode(LOG_ENCODING)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if _log_size() + len(record) > MAX_LOG_BYTES:
        _rotate_log()
    with LOG.open("ab") as f:
        f.write(record)


# ---------------------------------------------------------------------------
# single-instance lock
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def acquire_lock() -> bool:
    """Return True if we own the lock. Refuse to start if a live supervisor
    already holds it."""
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text().strip())
        except ValueError:
            old = 0
        if old and _pid_alive(old) and old != os.getpid():
            log(f"refusing to start: supervisor already running (pid {old})")
            return False
        # stale pidfile (dead pid) — take it over
    PIDFILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    if PIDFILE.exists():
        try:
            if int(PIDFILE.read_text().strip()) == os.getpid():
                PIDFILE.unlink()
        except (ValueError, OSError):
            pass


def read_pid() -> int | None:
    if not PIDFILE.exists():
        return None
    try:
        return int(PIDFILE.read_text().strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# child process management (capture output, kill the whole tree on stop)
# ---------------------------------------------------------------------------
def _utc_stamp() -> str:
    """UTC timestamp for child log names; microseconds keep two spawns inside
    the same second from colliding."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _prune_child_logs(child_dir: Path) -> None:
    """Delete the oldest child logs so the dir cannot grow past MAX_CHILD_LOGS.

    Names start with a UTC timestamp, so lexical order is chronological order.
    Keeps MAX_CHILD_LOGS - 1 existing files: the one the caller is about to
    create is the last one allowed. Never raises — a stale file we cannot
    unlink must not stop a spawn; the next prune retries it.
    """
    try:
        logs = sorted((p for p in child_dir.iterdir() if p.is_file()),
                      key=lambda p: p.name)
    except OSError:
        return
    keep = max(MAX_CHILD_LOGS - 1, 0)
    for stale in logs[:max(len(logs) - keep, 0)]:
        try:
            stale.unlink()
        except OSError:
            pass


class ChildTracker:
    """Track the current child (harness.py) and its pi grandchild so we can
    kill the whole tree on stop."""

    def __init__(self):
        self.current: subprocess.Popen | None = None

    def spawn(self, args: list[str], *, label: str) -> int:
        """Run a harness subcommand, tracking it. Returns its exit code.

        stdout and stderr share one file handle under
        <WORK_DIR>/logs/children/ so their relative order is kept and a
        traceback or pre-kill output is no longer lost to /dev/null.
        """
        child_dir = WORK_DIR / "logs" / "children"
        child_dir.mkdir(parents=True, exist_ok=True)
        _prune_child_logs(child_dir)
        log_path = child_dir / f"{_utc_stamp()}-{label}.log"
        log(f"  ▶ child '{label}' output -> {log_path}")
        with log_path.open("w", encoding=LOG_ENCODING) as out:
            out.write(f"=== spawn {label} args={args} ===\n")
            out.flush()
            try:
                self.current = subprocess.Popen(
                    args, cwd=HARNESS_DIR, stdout=out, stderr=out,
                    start_new_session=True)  # own process group -> killable as a tree
                rc = self.current.wait()
            finally:
                self.current = None
            out.write(f"=== exited rc={rc} ===\n")
            out.flush()
        return rc

    def kill_tree(self) -> None:
        """Kill the current child and its entire process group."""
        proc = self.current
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
def _queue_snapshot(provider: TaskProvider,
                    lifecycle: TaskLifecycle) -> QueueCounts:
    """Read the three counts one cycle's decision is made from.

    Read-only: `claim=False` is spelled out because a call that only counts must
    never be one default-flip away from moving the queue. The supervisor takes
    one snapshot before the cycle's child runs and another after it exits;
    `QueueCounts` compares by value, so the pair is the progress test (T15).
    """
    return QueueCounts(pending=len(provider.fetch_pending(claim=False)),
                       in_flight=len(in_flight_task_dirs(lifecycle)),
                       claims=len(provider.list_claims()))


def run_loop() -> int:
    if not acquire_lock():
        return 1
    tracker = ChildTracker()
    stop = {"flag": False}

    def handle_term(signum, frame):
        log(f"received signal {signum}; stopping after current step")
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    log(f"supervisor started (pid {os.getpid()}, sleep={SLEEP_S}s, "
        f"max_cycles={MAX_CYCLES}, fail_limit={FAIL_LIMIT})")

    # Built once for the whole loop: the queue state is re-read every cycle,
    # the provider, the lifecycle and the config behind them never change, and
    # rebuilding them per cycle is what used to make only one count cheap
    # enough to log.
    cfg = load(CONFIG_PATH)
    provider = create_provider(cfg)
    lifecycle = TaskLifecycle(cfg, log=log)

    cycle = 0
    failcount = 0
    idle_streak = 0   # consecutive cycles that changed nothing (T15 backoff)
    try:
        while not stop["flag"]:
            if STOPFILE.exists():
                log("STOP file present; halting")
                STOPFILE.unlink()
                break
            cycle += 1
            if MAX_CYCLES > 0 and cycle > MAX_CYCLES:
                log(f"reached MAX_CYCLES={MAX_CYCLES}; halting")
                break

            # --- circuit breaker: can the harness even launch? ---
            rc = tracker.spawn([sys.executable, "harness.py", "status"],
                               label="status")
            if rc != 0:
                failcount += 1
                log(f"  ⚠ harness failed to launch ({failcount}/{FAIL_LIMIT})")
                if failcount >= FAIL_LIMIT:
                    log("  ⛔ CIRCUIT BREAKER: reverting trunk to pi/last-good")
                    # git_cli owns the revert (and the dirty-tree guard in front of
                    # it). A refusal is a log line, not a reason to die: the loop
                    # has to keep trying, and the worktree stays exactly where it is.
                    try:
                        reverted_to = revert_to_last_good(HARNESS_DIR, TRUNK)
                        log(f"  reverted to {reverted_to}")
                    except Exception as exc:
                        log(f"  ⚠ breaker refused: {exc}")
                    failcount = 0
                _sleep(stop, SLEEP_S)
                continue
            failcount = 0  # launched fine

            # --- decide: in-flight beats pending beats claims beats generate ---
            before = _queue_snapshot(provider, lifecycle)
            action = decide_cycle_action(before.pending, before.in_flight,
                                         before.claims)
            summary = cycle_summary(before.pending, before.in_flight,
                                    before.claims, action)
            log(f"── cycle {cycle}: {summary} ──")
            if action is CycleAction.BLOCKED:
                # The D4 state, now named instead of chased (T44): claims are
                # recoverable only by the operator command, since the stale-claim
                # requeue is opt-in. One line naming what to run and how many
                # claims it would move; nothing here fails, moves or requeues a
                # claim, and the backoff below bounds what the wait costs.
                log(f"  ⚠ blocked: {before.claims} claim(s) in claimed/ with "
                    "pending/ and active/ empty — no child can work them; "
                    "preview the operator recovery with "
                    "harness.py requeue-claims --dry-run")
            cmd = command_for_action(action, sys.executable)
            if cmd:   # BLOCKED has no child: this cycle spawns nothing
                # The label is the subcommand, not the action: two actions share
                # `run-task-loop`, and the name a human tails or reruns is the
                # subcommand (T08 item 5).
                rc = tracker.spawn(list(cmd),
                                   label=subcommand_for_action(action))
                if rc != 0:
                    log(f"  {action.value} child exited rc={rc}")

            # --- sleep: SLEEP_S after progress, doubling backoff after none ---
            # A blocked cycle ran no child, so its counts cannot move and it
            # lands on this same path: idle, not an error. The sleep stays the
            # interruptible _sleep(), so a stop is honoured mid-backoff.
            after = _queue_snapshot(provider, lifecycle)
            progressed = after != before
            idle_streak = 0 if progressed else idle_streak + 1
            if idle_streak >= 1:
                secs = backoff_seconds(idle_streak, SLEEP_S, MAX_SLEEP_S)
                log(f"  no progress (streak {idle_streak}); sleeping {secs}s")
                _sleep(stop, secs)
            else:
                _sleep(stop, SLEEP_S)
    finally:
        tracker.kill_tree()
        release_lock()
        log("supervisor exited")
    return 0


def _sleep(stop: dict, seconds: int) -> None:
    """Interruptible sleep."""
    end = time.monotonic() + seconds
    while not stop["flag"] and time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


# ---------------------------------------------------------------------------
# daemonize for `start`
# ---------------------------------------------------------------------------
def daemonize() -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    # redirect stdio to the log
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    logf = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.dup2(logf, sys.stdout.fileno())
    os.dup2(logf, sys.stderr.fileno())


def cmd_start() -> int:
    if read_pid() and _pid_alive(read_pid()):
        log(f"already running (pid {read_pid()})")
        return 0
    daemonize()
    return run_loop()


def cmd_stop() -> int:
    pid = read_pid()
    if not pid or not _pid_alive(pid):
        print("supervisor not running")
        # clear stale pidfile
        if PIDFILE.exists():
            PIDFILE.unlink()
        return 0
    os.kill(pid, signal.SIGTERM)
    # wait for it to exit
    for _ in range(30):
        if not _pid_alive(pid):
            break
        time.sleep(1)
    if _pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    release_lock()
    print(f"supervisor stopped (pid {pid})")
    return 0


def cmd_status() -> int:
    pid = read_pid()
    if pid and _pid_alive(pid):
        print(f"supervisor running (pid {pid})")
    else:
        print("supervisor not running")
        if PIDFILE.exists():
            PIDFILE.unlink()
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "run":
        return run_loop()
    if cmd == "start":
        return cmd_start()
    if cmd == "stop":
        return cmd_stop()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
