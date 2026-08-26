"""Command handlers for the harness CLI."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..workflow.autonomous import AutonomousGenerator
from ..workflow.resume import resume_task
from ..core.providers import Task
from ..core.stats import render_report
from ..composition import build


def _log(line: str = "") -> None:
    print(line, flush=True)


def _slug(name: str) -> str:
    return (re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_")[:60] or "task")


def _requeue_claimed(provider, task) -> None:
    """Move a claimed-but-unprocessed task back to pending."""
    claimed = getattr(provider, "claimed_dir", None)
    pending = getattr(provider, "pending_dir", None)
    if not claimed or not pending:
        return
    for f in claimed.glob("*.md"):
        if _slug(f.stem) == task.id:
            f.rename(pending / f.name)
            _log(f"  requeued unprocessed claim: {task.id}")
            break


def cmd_run_task(file: str) -> int:
    cfg, store, runner, provider, pipeline = build()
    task = Task(id=_slug(Path(file).stem), body=Path(file).read_text(),
                source=f"cli:{file}")
    pipeline.process(task)
    return 0


def cmd_run() -> int:
    cfg, store, runner, provider, pipeline = build()
    tasks = provider.fetch_pending(claim=True)
    if not tasks:
        _log("no pending tasks")
    for task in tasks:
        pipeline.process(task)
    # autonomous mode when queue drains
    remaining = provider.fetch_pending()
    if not remaining:
        _log("queue empty -> entering autonomous mode")
        gen = AutonomousGenerator(cfg, runner, provider, log=_log)
        # generate against the harness's own repo (self-improvement)
        gen.run(Path(__file__).resolve().parent)
    return 0


def cmd_run_one() -> int:
    """Claim and process exactly ONE pending task, then exit.

    The supervisor calls this once per cycle. Claiming a single task (not the
    whole queue) means the claimed/ staging dir never accumulates unprocessed
    tasks — each is claimed, processed, and released within one invocation.
    """
    cfg, store, runner, provider, pipeline = build()
    tasks = provider.fetch_pending(claim=True)
    if not tasks:
        _log("no pending tasks to claim")
        return 0
    task = tasks[0]
    _log(f"processing {task.id} ({len(tasks)} claimed this cycle)")
    pipeline.process(task)
    # release any other claims made this cycle that we did not process,
    # returning them to pending so a future cycle picks them up.
    for other in tasks[1:]:
        _requeue_claimed(provider, other)
    return 0


def cmd_run_task_loop() -> int:
    """Process pending tasks one at a time until the queue is empty.

    Kept for manual use (`harness.py run-task-loop`). The supervisor uses
    `run-one` instead, one task per cycle.
    """
    cfg, store, runner, provider, pipeline = build()
    while True:
        tasks = provider.fetch_pending(claim=True)
        if not tasks:
            _log("pending queue empty")
            return 0
        task = tasks[0]
        _log(f"processing {task.id} ({len(tasks)} pending)")
        pipeline.process(task)
        for other in tasks[1:]:
            _requeue_claimed(provider, other)


def cmd_autonomous() -> int:
    cfg, store, runner, provider, pipeline = build()
    gen = AutonomousGenerator(cfg, runner, provider, log=_log)
    gen.run(Path(__file__).resolve().parent)
    return 0


def cmd_status() -> int:
    cfg, store, runner, provider, pipeline = build()
    for sub in ("pending", "active", "done", "failed", "parked", "review"):
        d = cfg.queue_dir / sub
        items = sorted(p.name for p in d.iterdir()) if d.exists() else []
        _log(f"{sub:<10} ({len(items)}): {', '.join(items) if items else '-'}")
    _log()
    _log(render_report(store.all()))
    return 0


def cmd_report() -> int:
    _, store, *_ = build()
    print(render_report(store.all()))
    return 0


def cmd_resume(task_id: str, yes: bool = False) -> int:
    """Resume a task from its last checkpoint (spec FR3)."""
    cfg, store, runner, provider, pipeline = build()
    return resume_task(task_id, yes, cfg, pipeline,
                       lifecycle=pipeline.lifecycle, log=_log)


def cmd_unpark(task_id: str) -> int:
    """Move a parked (or failed) task back to pending so it is re-processed.

    The task's artifacts (spec, slices, progress) are preserved, so the next
    run continues from where it got to rather than starting over.
    """
    cfg, *_ = build()
    moved = False
    for src_folder in ("parked", "failed"):
        src = cfg.queue_dir / src_folder / task_id
        if src.exists():
            dst = cfg.queue_dir / "pending" / f"{task_id}.md"
            original = src / "original.md"
            if original.exists():
                dst.write_text(original.read_text())
            else:
                dst.write_text(f"# {task_id}\n\n(requeued from {src_folder}; original requirement missing)\n")
            # remove the old terminal dir so it starts fresh in active/
            shutil.rmtree(src)
            # drop any stale exec summary
            (cfg.queue_dir / "review" / f"{task_id}.md").unlink(missing_ok=True)
            _log(f"unparked {task_id}: {src_folder} -> pending/{task_id}.md")
            moved = True
    if not moved:
        _log(f"{task_id} not found in parked/ or failed/")
        return 1
    return 0