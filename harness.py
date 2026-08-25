#!/usr/bin/env python3
"""CLI entry point for the autonomous workflow harness.

Usage:
  harness.py run                 Process all pending tasks, then autonomous mode
  harness.py run-task <file>     Process a single task file
  harness.py autonomous          Generate tasks until queue has N
  harness.py status              Show queue + stats
  harness.py report              Print the stats report
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.workflow.autonomous import AutonomousGenerator          # noqa: E402
from harness.core.config import load                             # noqa: E402
from harness.workflow.pipeline import Pipeline                       # noqa: E402
from harness.core.providers import Task, create_provider         # noqa: E402
from harness.core.session import SessionRunner                   # noqa: E402
from harness.core.stats import StatsStore, render_report         # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _log(line: str = "") -> None:
    print(line, flush=True)


def build(cfg_path: Path = CONFIG_PATH):
    cfg = load(cfg_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("pending", "active", "done", "failed", "parked", "review"):
        (cfg.queue_dir / sub).mkdir(parents=True, exist_ok=True)
    store = StatsStore(cfg.stats_path)
    runner = SessionRunner(cfg, store, log=_log)
    provider = create_provider(cfg)
    pipeline = Pipeline(cfg, runner, log=_log, provider=provider)
    return cfg, store, runner, provider, pipeline


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


def _slug(name: str) -> str:
    import re
    return (re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_")[:60] or "task")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd, rest = args[0], args[1:]
    if cmd == "run" and not rest:
        return cmd_run()
    if cmd == "run-task" and rest:
        return cmd_run_task(rest[0])
    if cmd == "run-one":
        return cmd_run_one()
    if cmd == "run-task-loop":
        return cmd_run_task_loop()
    if cmd == "autonomous":
        return cmd_autonomous()
    if cmd == "status":
        return cmd_status()
    if cmd == "report":
        return cmd_report()
    if cmd in ("unpark", "requeue") and rest:
        return cmd_unpark(rest[0])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
