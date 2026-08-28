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
from pathlib import Path

from ..core.config import Config
from ..core.enums import CheckpointStage, TaskStatus
from ..core.gitops import ensure_branch

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
    """
    id: str
    status: str = TaskStatus.ACTIVE.value
    source: str = ""
    created: str = ""
    stage: str = CheckpointStage.SPEC.value
    history: list = field(default_factory=list)
    checkpointed_stages: list = field(default_factory=list)
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
            "last_updated": self.last_updated,
            "workdir": self.workdir,
        }, indent=2)


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


class TaskLifecycle:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log

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
        return task_dir

    def park(self, task_id: str, reason: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "parked" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._stamp_status(task_id, TaskStatus.PARKED, "parked")
        self._exec_summary(task_id, "PARKED", reason, "parked")
        self.log(f"  task {task_id} PARKED: {reason}")

    def fail(self, task_id: str, reason: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "failed" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._stamp_status(task_id, TaskStatus.FAILED, "failed")
        self._exec_summary(task_id, "KICKED OUT", reason, "failed")
        self.log(f"  task {task_id} FAILED: {reason}")

    def complete(self, task_id: str, summary: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "done" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._stamp_status(task_id, TaskStatus.DONE, "done")
        self._exec_summary(task_id, "DONE", summary, "done")
        self.log(f"  task {task_id} DONE")

    def _stamp_status(self, task_id: str, status: TaskStatus, where: str) -> None:
        """Rewrite `task.json` at its *new* location so `status` agrees with the
        directory it landed in. Called only after the move succeeded. A missing
        or corrupt file yields a minimal valid state instead of raising."""
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

    def _exec_summary(self, task_id: str, status: str, text: str, where: str) -> None:
        td = self.cfg.queue_dir / where / task_id
        original = (td / "original.md").read_text() if (td / "original.md").exists() else ""
        review_dir = self.cfg.queue_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / f"{task_id}.md").write_text(f"""# Task: {task_id}

**Status:** {status}
**Date:** {_now()}

## Original requirement

{original}

## Executive summary

{text}

## Artifacts

- spec: `{td}/artifacts/spec.md`
- slices: `{td}/artifacts/slices.md`
- session outputs: `{td}/artifacts/*.out`
""")
        self.log(f"  exec summary: {review_dir / (task_id + '.md')}")

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
        """If the task references an existing git repo, work there; else the
        task dir. A missing `original.md` (partial crash, EC13) falls back to
        the task dir rather than crashing."""
        original = task_dir / "original.md"
        if not original.exists():
            self.log(f"  ⚠ {original} missing; using task dir as workdir")
            return task_dir
        for m in re.findall(r"/[a-zA-Z0-9_./-]+", original.read_text()):
            p = Path(m)
            if p.is_dir() and (p / ".git").exists():
                return p
        return task_dir
