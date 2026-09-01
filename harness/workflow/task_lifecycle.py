"""Task lifecycle management: queue moves, review summaries, and checkpoint state.

`task.json` is the single source of checkpoint truth (spec FR1). All writes go
through `write_atomic` (temp file in the same dir + `os.replace`) so a crash
never leaves a partial file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ..core.config import Config
from ..core.enums import CheckpointStage, Stage, TaskStatus

# Queue subdirectories that may hold a task dir.
QUEUE_LOCATIONS = ("active", "parked", "failed", "done")

# The one queue subdirectory that holds claims instead of task dirs.
CLAIMED_LOCATION = "claimed"

# Every queue subdirectory the harness creates or reports on, in lifecycle
# order: a task is written to pending/, held in claimed/ while a run owns it,
# worked in active/, then lands in review/, parked/, failed/ or done/.
QUEUE_LOCATIONS_ALL = ("pending", CLAIMED_LOCATION, "active", "review",
                       "parked", "failed", "done")


@dataclass
class TaskState:
    """The shape of `task.json` (spec F1.1).

    `checkpointed_stages` is an ordered prefix of CHECKPOINT_ORDER: the stages
    that completed successfully. `stage` is the stage currently in flight
    (informational; only `checkpointed_stages` drives skip decisions, F1.4).

    `checkpointed_slices` holds the ids of slices that passed their last
    required review (F8). They are plain strings exactly as `_parse_slices`
    yields them (`"3"`, `"3.1"`) — never ints, never re-formatted — in
    completion order, which is not necessarily `slices.md` order.
    """
    id: str
    status: str = TaskStatus.ACTIVE.value
    source: str = ""
    created: str = ""
    stage: str = CheckpointStage.SPEC.value
    history: list = field(default_factory=list)
    checkpointed_stages: list = field(default_factory=list)
    checkpointed_slices: list = field(default_factory=list)
    last_updated: str = ""
    # Recorded at intake and never re-derived (F7). Kept last so positional
    # construction of the earlier fields keeps working.
    workdir: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "status": self.status,
            "source": self.source,
            "created": self.created,
            "stage": self.stage,
            "history": self.history,
            "checkpointed_stages": [s.value for s in self.checkpointed_stages],
            "checkpointed_slices": list(self.checkpointed_slices),
            "last_updated": self.last_updated,
            "workdir": self.workdir,
        }, indent=2)


@dataclass
class Handoff:
    """What a parked over-cap task hands to the next agent (T75).

    The shape of the `## Handoff` section `TaskLifecycle.park` renders into the
    review file when a session was stopped for crossing the context cap. Every
    field except the two checkpointed lists comes off the caught
    `OverContextBudget` — the session that tripped is the only place those
    values exist — while `checkpointed_stages` and `checkpointed_slices` are
    the task's resume position read from `task.json` at the park site, so the
    next agent can see how far the run got before it stopped.

    Rendering lives in `_handoff_section`, not here (CODING_STANDARDS §2): this
    is the data shape only.
    """
    stage: Stage | str
    slice_id: str | None = None
    iteration: int = 1
    peak_tokens: int = 0
    context_limit: int | None = None
    output_path: Path | None = None
    checkpointed_stages: list = field(default_factory=list)
    checkpointed_slices: list = field(default_factory=list)


def _handoff_value(value) -> str:
    """One handoff field as text: an enum member renders its wire value, `None`
    renders `none` (never the literal `None`), anything else its `str`."""
    if value is None:
        return "none"
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _handoff_list(values) -> str:
    """A checkpointed list as one line: empty renders `none`, otherwise the
    members' wire values joined in order."""
    if not values:
        return "none"
    return ", ".join(_handoff_value(v) for v in values)


def _handoff_section(handoff: Handoff) -> str:
    """The `## Handoff` + `## Next agent should` block appended to a parked
    over-cap review file (T75), one line per field.

    A park with no handoff renders none of this — the plain review file stays
    byte-identical to what it rendered before T75.
    """
    lines = [
        "## Handoff",
        "",
        f"- stage: {_handoff_value(handoff.stage)}",
        f"- slice: {_handoff_value(handoff.slice_id)}",
        f"- iteration: {_handoff_value(handoff.iteration)}",
        f"- peak: {_handoff_value(handoff.peak_tokens)}",
        f"- cap: {_handoff_value(handoff.context_limit)}",
        f"- output: {_handoff_value(handoff.output_path)}",
        f"- checkpointed_stages: {_handoff_list(handoff.checkpointed_stages)}",
        f"- checkpointed_slices: {_handoff_list(handoff.checkpointed_slices)}",
        "",
        "## Next agent should",
        "",
        "re-split the work or reduce its context before resume",
        "",
    ]
    return "\n" + "\n".join(lines)


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (F1.2): temp file in the same
    directory, then `os.replace`. Readers never see a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_stages(raw: list, log=print) -> list:
    """Normalize a `checkpointed_stages` list from disk (F1.3, EC3, EC4).

    Unknown names are dropped with a warning; duplicates are removed while
    preserving first-seen order.
    """
    known = {s.value: s for s in CheckpointStage}
    seen: list = []
    for entry in raw:
        stage = known.get(entry)
        if stage is None:
            log(f"  ⚠ unknown checkpoint stage {entry!r} in task.json; ignoring")
            continue
        if stage not in seen:
            seen.append(stage)
    return seen


def _parse_completed_slices(raw, log=print) -> list:
    """Normalize a `checkpointed_slices` list from disk (F8).

    Mirrors `_parse_stages`' rules, with two differences dictated by the data:
    the entries are opaque slice id strings rather than enum members, and
    insertion order is the real completion order, so it is preserved exactly
    (dedupe keeps the first occurrence). Non-string entries are dropped with a
    warning — an int on disk means something wrote ids wrong, and re-formatting
    them here (`3.10` -> `3.1`) would silently mismatch later comparisons.
    """
    seen: list = []
    for entry in raw:
        if not isinstance(entry, str):
            log(f"  ⚠ non-string checkpointed slice {entry!r} in task.json; ignoring")
            continue
        if entry not in seen:
            seen.append(entry)
    return seen


class TaskLifecycle:
    def __init__(self, cfg: Config, log=print, handoff_sync=None,
                 stage_change_sync=None):
        self.cfg = cfg
        self.log = log
        # Optional GitHub handoff hook (spec FR-2.5, FR-3): a callable
        # `(task_id, stage, prose, slice_id, iteration)`. Built by the
        # composition root only when GitHub sync is enabled; it posts the
        # handoff comment and runs the in-flight sync pass. None keeps
        # every write site a no-op (FR-0.1). The hook swallows its own
        # failures (NFR-1), so this layer imports nothing from the sync.
        self.handoff_sync = handoff_sync
        # Optional GitHub stage-change hook (spec FR-3): a callable
        # `(task_id)` wired by the composition root to the sync dispatcher.
        # Every queue-location transition calls it once, after the move.
        # None (GitHub unconfigured) keeps every hook a no-op (FR-0.1,
        # NFR-2); the hook swallows its own failures (NFR-1), so a sync
        # error can never change a move.
        self.stage_change_sync = stage_change_sync

    # ------------------------------------------------------------------
    # dirs
    # ------------------------------------------------------------------
    def task_dir(self, task_id: str, where: str = "active") -> Path:
        return self.cfg.queue_dir / where / task_id

    def task_json_path(self, task_id: str, where: str = "active") -> Path:
        return self.task_dir(task_id, where) / "task.json"

    # ------------------------------------------------------------------
    # checkpoint state (F1.1-F1.5)
    # ------------------------------------------------------------------
    def load_state(self, task_id: str, where: str = "active") -> TaskState:
        """Read `task.json` from a queue subdirectory.

        Tolerant of pre-feature files (missing fields -> defaults, F1.4) and
        corrupt JSON (EC2: warn, treat as no checkpoints). A missing file
        raises FileNotFoundError; callers check existence first.
        """
        path = self.task_json_path(task_id, where)
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.log(f"  ⚠ task.json for {task_id} is unparseable; "
                     f"treating as no checkpoints")
            raw = {}
        if not isinstance(raw, dict):
            self.log(f"  ⚠ task.json for {task_id} is malformed; "
                     f"treating as no checkpoints")
            raw = {}
        return TaskState(
            id=raw.get("id", task_id),
            status=raw.get("status", TaskStatus.ACTIVE.value),
            source=raw.get("source", ""),
            created=raw.get("created", ""),
            stage=raw.get("stage", CheckpointStage.SPEC.value),
            history=list(raw.get("history", [])),
            checkpointed_stages=_parse_stages(raw.get("checkpointed_stages", []), self.log),
            checkpointed_slices=_parse_completed_slices(
                raw.get("checkpointed_slices") or [], self.log),
            last_updated=raw.get("last_updated", ""),
            workdir=raw.get("workdir", ""),
        )

    def save_state(self, state: TaskState, where: str = "active") -> None:
        """Atomically write `task.json` (F1.2)."""
        write_atomic(self.task_json_path(state.id, where), state.to_json())

    def checkpoint(self, task_id: str, stage: CheckpointStage, where: str = "active") -> None:
        """Record a stage as completed (F1.3): idempotent ordered append,
        `last_updated` bump, atomic save."""
        state = self.load_state(task_id, where)
        if stage not in state.checkpointed_stages:
            state.checkpointed_stages.append(stage)
        state.last_updated = _now()
        self.save_state(state, where)

    def checkpoint_slices(self, task_id: str, slice_ids, where: str = "active") -> None:
        """Record slices as completed (F8): append-only, dedupe, `last_updated`
        bump, atomic save.

        Same read-modify-write discipline as `checkpoint()`. Slice ids are
        stored verbatim — the resume decision compares them against what
        `_parse_slices` yields, so nothing here re-formats them.
        """
        state = self.load_state(task_id, where)
        for sid in _parse_completed_slices(list(slice_ids), self.log):
            if sid not in state.checkpointed_slices:
                state.checkpointed_slices.append(sid)
        state.last_updated = _now()
        self.save_state(state, where)

    def set_stage(self, task_id: str, stage: CheckpointStage, where: str = "active") -> None:
        """Mark `stage` as in flight (F1.1): set `stage`, bump `last_updated`,
        atomic save."""
        state = self.load_state(task_id, where)
        state.stage = stage.value
        state.last_updated = _now()
        self.save_state(state, where)

    # ------------------------------------------------------------------
    # queue moves
    # ------------------------------------------------------------------
    def intake(self, task) -> Path:
        """Create a fresh active/ dir for `task` and write its initial
        `task.json` (F1.5: new fields, signature unchanged)."""
        task_dir = self.task_dir(task.id)
        (task_dir / "artifacts" / "progress").mkdir(parents=True, exist_ok=True)
        (task_dir / "artifacts" / "sessions").mkdir(parents=True, exist_ok=True)
        (task_dir / "prompts").mkdir(exist_ok=True)
        (task_dir / "original.md").write_text(task.body)
        now = _now()
        state = TaskState(
            id=task.id,
            status=TaskStatus.ACTIVE.value,
            source=task.source,
            created=now,
            stage=CheckpointStage.SPEC.value,
            history=[],
            checkpointed_stages=[],
            last_updated=now,
        )
        self.save_state(state)
        # original.md exists from here on, so the workdir can be resolved and
        # recorded before any git or session work starts (F7).
        self.record_workdir(task_dir)
        self._sync_stage_change(task.id)
        return task_dir

    def park(self, task_id: str, reason: str,
             handoff: Handoff | None = None,
             from_: str = "active") -> None:
        """Park a task. `handoff` (T75) is passed only by the over-cap park site
        in `Pipeline.process`; when present its sections are appended to the
        review file. A park without one renders exactly what it rendered before.

        `from_` is the queue location the task moves *from* (GitHub-sync park,
        spec FR-1.3/FR-2.2): a task staged in `pending/`, `claimed/`, `review/`,
        `done/` or `failed/` parks through the same status-stamp/summary path
        as an active one. A task file parks as a task dir whose `original.md`
        is the file's content; a task dir moves whole."""
        self._terminal_move(task_id, TaskStatus.PARKED, "parked", "PARKED",
                            reason, handoff=handoff, from_=from_)
        self.log(f"  task {task_id} PARKED: {reason}")
        self._sync_stage_change(task_id)

    def fail(self, task_id: str, reason: str) -> None:
        self._terminal_move(task_id, TaskStatus.FAILED, "failed", "KICKED OUT", reason)
        self.log(f"  task {task_id} FAILED: {reason}")
        self._sync_stage_change(task_id)

    def complete(self, task_id: str, summary: str) -> None:
        self._terminal_move(task_id, TaskStatus.DONE, "done", "DONE", summary)
        self.log(f"  task {task_id} DONE")
        self._sync_stage_change(task_id)

    def _sync_stage_change(self, task_id: str) -> None:
        """Fire the GitHub stage-change hook once, after a move (spec FR-3).

        Last act of every transition, and the hook itself never raises
        (NFR-1): a sync failure is logged there and changes nothing about
        the move that already happened. No hook wired (GitHub disabled)
        means no call at all (FR-0.1, NFR-2)."""
        if self.stage_change_sync is not None:
            self.stage_change_sync(task_id)

    def _terminal_move(self, task_id: str, status: TaskStatus, where: str,
                       summary_status: str, text: str,
                       handoff: Handoff | None = None,
                       from_: str = "active") -> None:
        """Move a task dir into its terminal queue subdirectory, then record it.

        The move is the lifecycle authority, so it runs first and its failures
        propagate untouched: a task that never moved is not terminal, and
        writing bookkeeping somewhere else would create a false terminal record.

        Once the move has succeeded the task *is* terminal, whatever the writes
        do next. A bookkeeping failure is therefore logged and swallowed — an
        exception escaping here would let a caller read the task as still active
        and attempt a second terminal transition (T45). The two steps are
        guarded separately so a failed `task.json` does not also lose the review
        summary, and vice versa.
        """
        src = self._terminal_source(task_id, from_)
        dst = self.cfg.queue_dir / where / task_id
        if src.is_dir():
            shutil.move(str(src), str(dst))
        elif src.is_file():
            # A task *file* (pending/, claimed/, review/) has no dir yet; the
            # terminal record is a dir, so the file lands in it as
            # `original.md` (spec FR-1.3).
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst / "original.md"))
        try:
            self._stamp_status(task_id, status, where)
        except OSError as exc:
            self.log(f"  ⚠ {task_id} is in {where}/ but the status write failed "
                     f"on {self.task_json_path(task_id, where)}: {exc}; "
                     f"task.json was not updated")
        try:
            self._exec_summary(task_id, summary_status, text, where, handoff=handoff)
        except OSError as exc:
            self.log(f"  ⚠ {task_id} is in {where}/ but the review summary write "
                     f"failed on {self.review_summary_path(task_id)}: {exc}; "
                     f"no review summary was written")
            return
        self._post_handoff(task_id, summary_status, text, handoff)

    def _post_handoff(self, task_id: str, summary_status: str, text: str,
                      handoff: Handoff | None) -> None:
        """Mirror this terminal event onto the task's issue (spec FR-2.5).

        A park-with-handoff comments the `Handoff` prose (the handoff event
        itself); every other terminal move comments the executive-summary
        text. No hook wired (GitHub disabled) means nothing happens here
        (FR-0.1); the hook swallows its own errors, and the task has left
        the in-flight locations with this move, so the pass for this event
        is the move's stage-change hook — never a second one (FR-3)."""
        if self.handoff_sync is None:
            return
        if handoff is not None:
            self.handoff_sync(task_id, _handoff_value(handoff.stage),
                              _handoff_section(handoff).strip(),
                              handoff.slice_id, handoff.iteration)
            return
        self.handoff_sync(task_id, summary_status, text, None, None)

    def _terminal_source(self, task_id: str, from_: str) -> Path:
        """Where `task_id` currently lives in `from_`: a task dir, or the bare
        task file (`pending/`, `claimed/`, `review/` stage tasks as `<id>.md`).

        The dir form wins when both exist; neither existing is not an error
        here — `_terminal_move` moves nothing and the caller's own existence
        check decides what a missing task means."""
        directory = self.task_dir(task_id, from_)
        if directory.is_dir():
            return directory
        return directory.with_name(f"{task_id}.md")

    def review_summary_path(self, task_id: str) -> Path:
        """The review summary file for `task_id` (one path, two users: the
        writer below and the failure log in `_terminal_move`)."""
        return self.cfg.queue_dir / "review" / f"{task_id}.md"

    def _stamp_status(self, task_id: str, status: TaskStatus, where: str) -> None:
        """Rewrite `task.json` at its *new* location so `status` agrees with the
        directory it landed in. Called only after the move succeeded. A missing
        or corrupt file yields a minimal valid state instead of raising; an I/O
        failure on the write propagates to `_terminal_move`, which decides."""
        try:
            state = self.load_state(task_id, where)
        except FileNotFoundError:
            state = TaskState(
                id=task_id,
                status=status.value,
                source="unknown",
                created=_now(),
            )
        state.status = status.value
        state.last_updated = _now()
        self.save_state(state, where)

    def _exec_summary(self, task_id: str, status: str, text: str, where: str,
                      handoff: Handoff | None = None) -> None:
        """Write the review summary for a task already in `where`. I/O failures
        propagate to `_terminal_move`, which decides.

        When `handoff` is present (an over-cap park, T75) its sections are
        appended after the artifacts block; when it is `None` the file is
        byte-identical to the pre-T75 summary."""
        td = self.cfg.queue_dir / where / task_id
        original = (td / "original.md").read_text() if (td / "original.md").exists() else ""
        review_dir = self.cfg.queue_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        body = f"""# Task: {task_id}

**Status:** {status}
**Date:** {_now()}

## Original requirement

{original}

## Executive summary

{text}

## Artifacts

- spec: `{td}/artifacts/spec.md`
- slices: `{td}/artifacts/slices.md`
- journey: `{td}/artifacts/journey.md`
- session transcripts: `{td}/artifacts/sessions/`
"""
        if handoff is not None:
            body += _handoff_section(handoff)
        self.review_summary_path(task_id).write_text(body)
        self.log(f"  exec summary: {self.review_summary_path(task_id)}")

    def record_workdir(self, task_dir: Path, where: str = "active") -> Path:
        """Resolve the workdir and persist it in `task.json` (F7).

        Two callers only: `intake`, immediately after it writes `original.md`
        and `task.json`, and `Pipeline.process` once while migrating a
        `task.json` that predates the field. The value therefore lands before
        `ensure_branch` or any session starts, and every later run reads it
        back instead of re-deriving it.
        """
        workdir = self.resolve_workdir(task_dir)
        state = self.load_state(task_dir.name, where)
        state.workdir = str(workdir)
        state.last_updated = _now()
        self.save_state(state, where)
        return workdir

    def resolve_workdir(self, task_dir: Path) -> Path:
        """Resolve the target repository workdir deterministically.

        Uses `cfg.repo_dir` (configured in config.json or passed via CLI).
        Falls back to `task_dir` (which the queue guard rejects) if no target
        repo is configured. Never extracts paths from markdown."""
        if self.cfg.repo_dir is not None:
            return self.cfg.repo_dir
        return task_dir
