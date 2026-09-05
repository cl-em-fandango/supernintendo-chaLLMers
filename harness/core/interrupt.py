"""The managed-interrupt state file: `<harnessExecutionAndQueueDir>/state/interrupt.json`.

One file, one owner: this module owns the shape of the interrupt record and
every read, write and delete of that file. Absence of the file means no
interrupt is active; presence means the harness must idle at its next session
boundary and only no-arg `harness.py resume` (or quick-mode completion) may
remove it. Distinct from the supervisor `STOP` file: STOP shuts the supervisor
down, interrupt keeps it alive and expects a resume.

Writes are atomic (temp file in the same directory + `os.replace`), so a crash
mid-write can never leave a half-written record for the loops to misread. A
record that cannot be parsed is treated as an active
`STAND_DOWN / REQUESTED` interrupt — the fail-safe direction is "the model
stays with the human" — with a warning naming the recovery command.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


STATE_SUBDIR = "state"
INTERRUPT_FILENAME = "interrupt.json"

# Emitted (through the caller's log) when the file exists but cannot be
# parsed. Names the recovery command, per the fail-safe contract: the reader
# still reports an active stand-down interrupt.
CORRUPT_INTERRUPT_WARNING = (
    "WARNING: interrupt state file is corrupt; treating it as an active "
    "STAND_DOWN interrupt (the model stays with the operator). "
    "Run `harness.py resume` to clear it and resume the harness.")


class InterruptMode(Enum):
    """Why the operator took the model: full stand-down or a quick borrow."""
    STAND_DOWN = "stand_down"
    QUICK = "quick"


class InterruptState(Enum):
    """Lifecycle of an active interrupt. `resuming` is log-only, never stored."""
    REQUESTED = "requested"
    PAUSED = "paused"


@dataclass
class InterruptStatus:
    """One interrupt record as read from (or written to) the state file.

    Timestamps are UTC ISO-8601 strings — the file format is an edge; inside
    the code the Enums above carry mode and state, never bare strings.
    """
    mode: InterruptMode
    state: InterruptState
    requested_at: str
    updated_at: str
    requester_pid: int = 0


def interrupt_path(work_dir: Path) -> Path:
    """The one location of the state file for a given harness execution and queue dir."""
    return Path(work_dir) / STATE_SUBDIR / INTERRUPT_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def write_interrupt(work_dir: Path, mode: InterruptMode,
                    state: InterruptState, requester_pid: int = 0) -> InterruptStatus:
    """Create the interrupt file atomically and return what was written."""
    now = _utc_now_iso()
    status = InterruptStatus(mode=mode, state=state, requested_at=now,
                             updated_at=now, requester_pid=requester_pid)
    _write_status(work_dir, status)
    return status


def _write_status(work_dir: Path, status: InterruptStatus) -> None:
    """Serialize one record to the state file, atomically (temp + rename)."""
    path = interrupt_path(work_dir)
    payload = json.dumps({
        "mode": status.mode.value,
        "state": status.state.value,
        "requested_at": status.requested_at,
        "updated_at": status.updated_at,
        "requester_pid": status.requester_pid,
    }, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, payload)


def acknowledge_interrupt(work_dir: Path,
                          log: Optional[Callable[[str], None]] = None
                          ) -> Optional[InterruptStatus]:
    """Acknowledge an active interrupt at a session boundary (FR-6.2).

    A run loop calls this where it would otherwise spawn the next `pi`
    session. When a file is present it transitions `requested -> paused`
    (mode, `requested_at` and `requester_pid` preserved, `updated_at`
    refreshed) and returns the record to report. The transition is
    last-writer-wins and safe: several acknowledging processes each leave
    the same `paused` record behind. A file already `paused` is returned
    unchanged, nothing rewritten. No file means no interrupt: None.

    A corrupt file reads fail-safe as STAND_DOWN/REQUESTED (with the
    recovery warning through `log`) and is rewritten as a clean `paused`
    record — the model stays with the operator either way. (The operator's
    original bytes are not preserved in that one case; the fail-safe
    direction — an active, readable stand-down — outranks forensics.)

    A concurrent no-arg `resume` deletes the file, and the delete can land
    between this function's read and its write. The ack therefore re-reads
    immediately before writing and skips the write when the request is
    gone, then verifies after writing and drops a resurrected record: a
    harness the operator just released must not be re-paused by a late ack.
    A dropped ack returns None — with no file there is no interrupt, and
    the caller continues (spec E8).
    """
    status = read_interrupt(work_dir, log=log)
    if status is None:
        return None
    if status.state is InterruptState.REQUESTED:
        ack = InterruptStatus(mode=status.mode, state=InterruptState.PAUSED,
                              requested_at=status.requested_at,
                              updated_at=_utc_now_iso(),
                              requester_pid=status.requester_pid)
        if read_interrupt(work_dir) is None:
            return None  # a resume deleted the request after the first read
        _write_status(work_dir, ack)
        if read_interrupt(work_dir) is None:
            # The delete landed between the re-read and the rename: undo
            # the resurrected record instead of re-pausing the harness.
            try:
                interrupt_path(work_dir).unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return ack
    return status


def _atomic_write(path: Path, payload: str) -> None:
    """Write via temp file + rename; leave no temp litter on failure."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_interrupt(work_dir: Path,
                   log: Optional[Callable[[str], None]] = None
                   ) -> Optional[InterruptStatus]:
    """Read the state file. None means no interrupt is active.

    A missing file is simply "no interrupt". A file that exists but cannot be
    parsed (unreadable, unparsable JSON, unknown enum values) is reported as
    an active STAND_DOWN/REQUESTED interrupt and `log`, when given, receives
    `CORRUPT_INTERRUPT_WARNING` naming the recovery command.
    """
    path = interrupt_path(work_dir)
    try:
        raw = json.loads(path.read_text())
        status = InterruptStatus(
            mode=InterruptMode(raw["mode"]),
            state=InterruptState(raw["state"]),
            requested_at=str(raw["requested_at"]),
            updated_at=str(raw["updated_at"]),
            requester_pid=int(raw.get("requester_pid", 0)),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        if log is not None:
            log(CORRUPT_INTERRUPT_WARNING)
        now = _utc_now_iso()
        return InterruptStatus(mode=InterruptMode.STAND_DOWN,
                               state=InterruptState.REQUESTED,
                               requested_at=now, updated_at=now,
                               requester_pid=0)
    return status


def clear_interrupt(work_dir: Path) -> bool:
    """Delete the state file. True if one was there, False if not."""
    try:
        interrupt_path(work_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def interrupt_age_seconds(status: InterruptStatus,
                          now: Optional[datetime] = None) -> float:
    """Seconds elapsed since the interrupt was requested."""
    current = now or datetime.now(timezone.utc)
    return (current - _parse_iso(status.requested_at)).total_seconds()
