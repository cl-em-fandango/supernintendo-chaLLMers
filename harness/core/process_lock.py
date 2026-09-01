"""PID-based single-instance file locks (spec FR-4.1, FR-4.3).

Two locks share this mechanism:

* `<workDir>/run.lock` — taken by a harness run command for its whole life;
  the daemon reads it to decide whether a harness is already running, and a
  hand-started run blocks spawning equally (FR-4.3).
* `<workDir>/syncd.lock` — the daemon's own single-instance lock; a second
  `harness syncd` exits non-zero while a live daemon holds it (AC-10).

A lock is a file whose only content is the holder's PID. A lock whose PID is
not a live process (or is unreadable) is stale: it is removed and the lock is
taken (AC-11 — a daemon killed with the file left behind recovers). The
write is create-exclusive, so two simultaneous acquirers cannot both win.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The run-path lock the daemon reads before spawning (FR-4.3).
RUN_LOCK_NAME = "run.lock"
# The daemon's single-instance lock (FR-4.1).
SYNCD_LOCK_NAME = "syncd.lock"


class StaleLockError(Exception):
    """A lock file exists but cannot be read or parsed; treated as stale."""


@dataclass(frozen=True)
class LockHolder:
    """The process a lock file names: the PID recorded in the file."""
    pid: int


class LockHeldError(Exception):
    """Acquire refused: a live process holds the lock."""

    def __init__(self, path: Path, holder: LockHolder):
        super().__init__(
            f"lock {path} is held by running pid {holder.pid}")
        self.path = path
        self.holder = holder


def pid_is_alive(pid: int) -> bool:
    """True when `pid` names a process this process may signal.

    `os.kill(pid, 0)` probes without sending: ESRCH means dead, EPERM means
    alive but owned by someone else — both readings are conservative in the
    safe direction (a live-looking PID is never stolen).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProcessLock:
    """One named lock file inside a working directory.

    State is on disk, not in this object: `acquire()` writes the file,
    `release()` removes it, `is_held()` reads it. A process that never
    acquired the lock can still report (and clean up) a stale one.
    """

    def __init__(self, work_dir: Path, name: str):
        self.work_dir = Path(work_dir)
        self.name = name

    @property
    def path(self) -> Path:
        return self.work_dir / self.name

    def acquire(self) -> None:
        """Take the lock, recovering a stale file; raise `LockHeldError`
        when a live PID holds it, `StaleLockError` when a stale file
        cannot be removed."""
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self._clear_if_stale()
                continue
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{os.getpid()}\n")
            return

    def release(self) -> None:
        """Remove the lock file; a missing file is already released."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def holder(self) -> LockHolder | None:
        """Who the file names, or None when there is no lock file.

        Raises `StaleLockError` when the file exists but cannot be read or
        parsed — an unreadable lock is evidence of *something*, and the
        caller decides (acquire treats it as stale, `is_held` as held).
        """
        try:
            content = self.path.read_text().strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StaleLockError(
                f"lock {self.path} is unreadable: {exc}") from exc
        try:
            return LockHolder(pid=int(content))
        except ValueError:
            raise StaleLockError(
                f"lock {self.path} does not name a pid: {content!r}")

    def is_held(self) -> bool:
        """True when the lock names a live process.

        A stale file (dead or unreadable PID) reads as free: the next
        `acquire()` removes it. An unreadable file is reported on the
        harness log sink by the caller that owns one; this leaf has no
        log, so it reports the conservative answer only.
        """
        try:
            holder = self.holder()
        except StaleLockError:
            return False
        return holder is not None and pid_is_alive(holder.pid)

    def _clear_if_stale(self) -> None:
        """Remove the file when its PID is dead or unreadable.

        A corrupt file is treated as stale (it can only be left by a dead
        writer — a live holder always writes a readable PID). A lock whose
        PID is alive is never touched.
        """
        try:
            holder = self.holder()
        except StaleLockError:
            holder = None
        if holder is not None and pid_is_alive(holder.pid):
            raise LockHeldError(self.path, holder)
        self._clear_stale_file(holder)

    def _clear_stale_file(self, expected: LockHolder | None) -> None:
        """Remove the lock only while it still names `expected` (the holder
        the caller read as stale) or is unreadable.

        Between that read and this unlink, a concurrent acquirer may have
        cleared the stale file and installed a fresh lock; an unconditional
        unlink would steal it. Re-reading and verifying before unlinking
        narrows the window to the last check — the residual microseconds
        are accepted (the full fix is flock, which would tie the lock to
        an open fd for the holder's whole life). A file that changed under
        us is left alone: the `acquire()` loop re-reads it on the next
        iteration and reacts to what is there now.
        """
        try:
            current = self.holder()
        except StaleLockError:
            current = None
        if current is not None and pid_is_alive(current.pid):
            raise LockHeldError(self.path, current)
        if (current is not None and expected is not None
                and current.pid != expected.pid):
            return  # a different stale PID appeared; re-evaluate next loop
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
