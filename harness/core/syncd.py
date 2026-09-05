"""The `harness syncd` daemon loop (spec FR-4, AC-9/AC-10/AC-11).

The daemon does exactly two things per pass (FR-4.4):

1. run a full two-way sync pass — skipped entirely when GitHub is
   unconfigured (`sync=None`); the daemon then is a local-work watcher only
   (FR-0.1: still zero HTTP);
2. spawn exactly one harness run when `pending/` is non-empty and no run is
   active. "Active" is the `run.lock` the run commands take (FR-4.3), plus
   the PID of the last child this daemon spawned (the gap between spawning
   and the child taking its own lock must not invite a second spawn).

Everything else is policy the caller injects through `SyncdParams`: the
sync callable, the spawn callable, the sleep, the pending check. Tests drive
the loop in-process with fakes (NFR-5); nothing here imports the pipeline.

Single instance (FR-4.1, AC-10): `<harnessExecutionAndQueueDir>/syncd.lock` with dead-PID stale
recovery (AC-11). Failure backoff (FR-4.5): at N consecutive failed sync
passes the interval backs off 5x and exactly one warning is logged per
backoff *entry*; a successful pass resets both the counter and the interval.
Signals (FR-4.6): `request_stop()` ends the loop after the current pass, the
lock is removed, exit is 0.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .process_lock import (
    LockHeldError,
    ProcessLock,
    RUN_LOCK_NAME,
    SYNCD_LOCK_NAME,
)

# The queue location the daemon watches for work (FR-4.2b). A directory
# name — a queue-location string at the edge, like the lifecycle's own.
PENDING_LOCATION = "pending"

# FR-4.5: N consecutive failed passes before backing off.
SYNC_FAILURE_THRESHOLD = 5
# FR-4.5: the backed-off interval is this many times the configured one.
BACKOFF_MULTIPLIER = 5


def _default_sleep(seconds: float, stop: Callable[[], bool]) -> None:
    """Interruptible sleep: wakes within 0.2s of the loop asking to stop."""
    deadline = time.monotonic() + max(float(seconds), 0.0)
    while not stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


@dataclass(frozen=True)
class SyncdParams:
    """The daemon's explicit parameters object (CODING_STANDARDS §5).

    * `sync` — runs one full sync pass; None when GitHub is unconfigured,
      which makes every pass skip straight to the work watch (FR-0.1).
    * `spawn` — starts one harness run and returns the child's PID.
    * `sleep` — waits up to `seconds`, returning early once the loop asks
      to stop (how FR-4.6's "finish the current pass" stays prompt).
    * `check_pending` — True when `pending/` holds at least one task file.
    * `stop_after_passes` — None is run forever; a number is the test and
      single-shot bound on the loop.
    """
    work_dir: Path
    sync_interval_s: float
    sync: Callable[[], object] | None = None
    spawn: Callable[[], int] | None = None
    log: Callable[[str], None] = print
    sleep: Callable[[float, Callable[[], bool]], None] = _default_sleep
    check_pending: Callable[[], bool] | None = None
    stop_after_passes: int | None = None


class SyncdLoop:
    """The daemon body: lock, poll, sync, spawn, release (FR-4).

    `run()` owns the whole life of the daemon and returns the process exit
    code: 1 with a lock message when a live daemon holds `syncd.lock`
    (AC-10), 0 after a clean (possibly signal-stopped) run with the lock
    removed (FR-4.6).
    """

    def __init__(self, params: SyncdParams):
        self.params = params
        self.daemon_lock = ProcessLock(params.work_dir, SYNCD_LOCK_NAME)
        self.run_lock = ProcessLock(params.work_dir, RUN_LOCK_NAME)
        self.stop_requested = False
        self._consecutive_failures = 0
        self._in_backoff = False
        self._spawned_pid: int | None = None

    def run(self) -> int:
        """Run the daemon until stopped or `stop_after_passes` passes."""
        try:
            self.daemon_lock.acquire()
        except LockHeldError as exc:
            self.params.log(f"syncd: {exc}; another syncd is running")
            return 1
        try:
            self._loop()
        finally:
            self.daemon_lock.release()
        return 0

    def request_stop(self) -> None:
        """Ask the loop to stop after its current pass (FR-4.6).

        Safe as a signal-handler body: it only flips a flag.
        """
        self.stop_requested = True

    @property
    def current_interval_s(self) -> float:
        """The poll interval in force: 5x while in backoff (FR-4.5)."""
        if self._in_backoff:
            return self.params.sync_interval_s * BACKOFF_MULTIPLIER
        return self.params.sync_interval_s

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        passes = 0
        while not self.stop_requested:
            self._sync_once()
            self._spawn_once()
            passes += 1
            if (self.params.stop_after_passes is not None
                    and passes >= self.params.stop_after_passes):
                return
            self._wait()
            if self.stop_requested:
                return

    def _sync_once(self) -> None:
        """One sync pass, failures logged and swallowed (NFR-1).

        A pass fails in two ways. It may raise, or it may return a report
        that says the pass was aborted: the production sync callable is
        the sync engine's dispatch, which never raises on GitHub errors
        (spec edge 9 — a spent rate-limit budget or an auth disable comes
        back as `SyncReport.aborted=True`), so the flag is the only
        failure signal FR-4.5 can see in production.
        """
        if self.params.sync is None:
            return  # GitHub unconfigured: local-work watcher only (FR-0.1)
        try:
            result = self.params.sync()
        except Exception as exc:  # noqa: BLE001 - NFR-1: the daemon outlives it
            self._note_sync_failure(f"{type(exc).__name__}: {exc}")
            return
        if _pass_aborted(result):
            reason = getattr(result, "abort_reason", "") or "no reason given"
            self._note_sync_failure(f"aborted: {reason}")
            return
        self._note_sync_success()

    def _spawn_once(self) -> None:
        """Spawn one harness run when there is work and no run is active."""
        if self.params.spawn is None or not self._has_pending_work():
            return
        if self.run_lock.is_held() or self._last_child_alive():
            return
        try:
            self._spawned_pid = self.params.spawn()
        except Exception as exc:  # noqa: BLE001 - NFR-1: retry next pass
            self.params.log(f"syncd: spawn failed: {type(exc).__name__}: {exc}")
            return
        self.params.log(f"syncd: spawned harness run pid {self._spawned_pid}")

    def _wait(self) -> None:
        self.params.sleep(self.current_interval_s,
                          lambda: self.stop_requested)

    # -- failure backoff (FR-4.5) -------------------------------------------

    def _note_sync_failure(self, detail: str) -> None:
        self._consecutive_failures += 1
        self.params.log(f"syncd: sync pass failed "
                        f"({self._consecutive_failures}/"
                        f"{SYNC_FAILURE_THRESHOLD}): {detail}")
        if (self._consecutive_failures == SYNC_FAILURE_THRESHOLD
                and not self._in_backoff):
            self._in_backoff = True
            self.params.log(
                f"syncd: backing off to {self.current_interval_s:g}s "
                f"after {self._consecutive_failures} failed sync passes")

    def _note_sync_success(self) -> None:
        """A successful pass resets the counter and the interval (FR-4.5)."""
        self._consecutive_failures = 0
        self._in_backoff = False

    # -- injected defaults ---------------------------------------------------

    def _has_pending_work(self) -> bool:
        if self.params.check_pending is not None:
            return self.params.check_pending()
        return _default_check_pending(self.params.work_dir)

    def _last_child_alive(self) -> bool:
        """True while the tracked spawned child runs; reaps it once it exits.

        The child is ours, so `os.waitpid(pid, WNOHANG)` both probes and
        reaps: an exited child must not linger as a zombie, or a plain
        `os.kill(pid, 0)` liveness probe would report it alive forever and
        the daemon would never spawn again (FR-4).

        A `ChildProcessError` means the pid is no longer a child of this
        process — already reaped, or never ours — which also reads as dead.
        """
        pid = self._spawned_pid
        if pid is None:
            return False
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            self._spawned_pid = None
            return False
        if reaped_pid == 0:
            return True  # still running
        self._spawned_pid = None
        return False


def _pass_aborted(result: object) -> bool:
    """True when a returned sync report says its pass was aborted.

    Duck-typed on the `aborted` attribute so the daemon stays decoupled
    from the sync module's report type (and its HTTP imports): a sync
    callable that returns None, or anything without the flag, reads as a
    successful pass.
    """
    return bool(getattr(result, "aborted", False))


def _default_check_pending(work_dir: Path) -> bool:
    """True when `pending/` holds at least one task file.

    Task files are markdown only. Task metadata lives in the dot-prefixed
    `.meta/` record store (FR-A4), so no transition can leave a record in
    `pending/` for this check to mistake for work. Hidden files are skipped
    for the same reason: they are editor droppings, not tasks.
    """
    pending = Path(work_dir) / "queue" / PENDING_LOCATION
    if not pending.is_dir():
        return False
    return any(not p.name.startswith(".") for p in pending.glob("*.md"))
