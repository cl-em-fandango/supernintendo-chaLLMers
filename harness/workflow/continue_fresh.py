"""Checkpoint-aware run flags: `--continue` (F4.2/F4.3) and `--fresh` (F4.4).

`--continue` resumes in-flight tasks left in `active/` (crash or previous
run) before processing the pending queue. `--fresh` deletes a task's
`active/` dir so a re-run starts from scratch instead of resuming.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..core.config import Config
from ..core.providers import Task
from .task_lifecycle import TaskLifecycle


def in_flight_task_dirs(lifecycle: TaskLifecycle) -> list[Path]:
    """Task dirs in `active/` that contain a `task.json` (F4.2).

    Orphan dirs without a `task.json` (crash between mkdir and the first
    state write) are left alone: they have no checkpoint to honor and no
    reliable way to reconstruct the pending source.
    """
    active = lifecycle.cfg.queue_dir / "active"
    if not active.exists():
        return []
    return sorted(d for d in active.iterdir()
                  if d.is_dir() and (d / "task.json").exists())


def task_from_dir(task_dir: Path, lifecycle: TaskLifecycle) -> Task:
    """Reconstruct a `Task` from an `active/` dir (F3.5): id = dir name,
    body = original.md contents (absent -> ""), source = task.json source
    (absent -> "resume")."""
    original = task_dir / "original.md"
    body = original.read_text() if original.exists() else ""
    source = "resume"
    state = lifecycle.load_state(task_dir.name)
    source = state.source or "resume"
    return Task(id=task_dir.name, body=body, source=source)


def resume_in_flight(lifecycle: TaskLifecycle, pipeline, log=print) -> int:
    """Resume every in-flight task in `active/` via `process()` (F4.2/F4.3).

    Returns the number of tasks resumed. Orphan dirs (no task.json) are
    skipped. A task that parks/fails during resume is left in its terminal
    dir; the remaining in-flight tasks still get their turn.
    """
    resumed = 0
    for task_dir in in_flight_task_dirs(lifecycle):
        resumed += 1
        log(f"  resuming in-flight task {task_dir.name}")
        pipeline.process(task_from_dir(task_dir, lifecycle))
    if resumed:
        log(f"resuming {resumed} in-flight task(s) from active/")
    return resumed


def fresh_restart(task_id: str, cfg: Config, log=print) -> None:
    """Delete `active/<id>/` and the stale `review/<id>.md` (F4.4), forcing
    a full restart of a task that would otherwise resume."""
    task_dir = cfg.queue_dir / "active" / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
        log(f"  --fresh: deleted {task_dir}")
    (cfg.queue_dir / "review" / f"{task_id}.md").unlink(missing_ok=True)
