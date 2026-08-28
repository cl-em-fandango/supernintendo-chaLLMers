"""Manual task recovery: `harness.py resume <task_id>` (spec FR3).

Finds a task dir in the queue (active/ -> parked/ -> failed/ -> done/),
prints the resume plan from `task.json`, and resumes it via `process()`
(resume path, FR2). Terminal tasks in done/ are reported, not resumed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..core.config import Config
from ..core.enums import CheckpointStage, Stage
from .continue_fresh import task_from_dir
from .pipeline import STAGE_SEQUENCE
from .task_lifecycle import QUEUE_LOCATIONS, TaskLifecycle


def _plan_stages() -> list[CheckpointStage]:
    """The checkpoint stages a resume may still run: the four pipeline stages
    plus the `merge` marker (T27). The terminal review is deliberately absent —
    it is never checkpointed (F1.1), so it is not a `CheckpointStage`; the plan
    preview appends it at the display site instead (T31)."""
    return list(STAGE_SEQUENCE) + [CheckpointStage.MERGE]


def _reason_from_review(review_file: Path) -> str:
    """Extract the park/fail reason from `review/<id>.md` (its "Executive
    summary" section); "(not recorded)" if absent."""
    if not review_file.exists():
        return "(not recorded)"
    text = review_file.read_text()
    marker = "## Executive summary\n"
    if marker not in text:
        return "(not recorded)"
    lines = [ln for ln in text.split(marker, 1)[1].splitlines() if ln.strip()]
    return lines[0].strip() if lines else "(not recorded)"


def _print_plan(task_id: str, where: str, lifecycle: TaskLifecycle, log=print) -> None:
    """Print the resume plan derived from task.json (F3.4)."""
    checkpointed: list[CheckpointStage] = []
    if lifecycle.task_json_path(task_id, where).exists():
        checkpointed = lifecycle.load_state(task_id, where=where).checkpointed_stages
    skipped = [s.value for s in checkpointed]
    # `MERGE` is a completion marker, not a stage a resume runs, so the preview
    # lists the pipeline stages only, then the terminal review — which is
    # terminal and never checkpointed, hence printed from `Stage` here rather
    # than from the checkpoint list (T31).
    will_run = [s.value for s in _plan_stages()
                if s not in checkpointed and s is not CheckpointStage.MERGE]
    if CheckpointStage.MERGE not in checkpointed:
        will_run.append(Stage.HOLISTIC.value)
    log(f"task {task_id} ({where})")
    log(f"  checkpointed: {', '.join(skipped) if skipped else '(none)'}")
    log(f"  will run:     {', '.join(will_run)}")


def confirm_resume(task_id: str, yes: bool, log=print, input_fn=input) -> bool:
    """Ask `Resume <id>? [Y/n]` (default yes). `--yes` skips the prompt."""
    if yes:
        return True
    try:
        answer = input_fn(f"Resume {task_id}? [Y/n] ").strip().lower()
    except EOFError:
        log("  (no input available; use --yes to resume non-interactively)")
        return False
    return answer in ("", "y", "yes")


def resume_task(task_id: str, yes: bool, cfg: Config, pipeline,
               lifecycle: TaskLifecycle, log=print, input_fn=input) -> int:
    """Run the full `resume <task_id>` flow (F3.2-F3.6). Returns the exit code."""
    # F3.2: search queue subdirs in order; first dir containing <id>/ wins.
    where = None
    for candidate in QUEUE_LOCATIONS:
        if lifecycle.task_dir(task_id, candidate).exists():
            where = candidate
            break
    if where is None:
        log(f"{task_id} not found in active/, parked/, failed/, done/")
        return 1

    task_dir = lifecycle.task_dir(task_id, where)

    if where == "done":
        log(f"{task_id} is already complete (merged to trunk); nothing to resume")
        return 0

    _print_plan(task_id, where, lifecycle, log)

    if where == "active":
        # F3.3: resume immediately, no prompt.
        pipeline.process(task_from_dir(task_dir, lifecycle))
        return 0

    # parked/ or failed/: show reason, confirm, move back to active/, resume.
    reason = _reason_from_review(cfg.queue_dir / "review" / f"{task_id}.md")
    log(f"  reason: {reason}")
    if not confirm_resume(task_id, yes, log=log, input_fn=input_fn):
        log(f"  not resuming {task_id}")
        return 0
    active_dir = lifecycle.task_dir(task_id, "active")
    active_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(task_dir), str(active_dir))
    (cfg.queue_dir / "review" / f"{task_id}.md").unlink(missing_ok=True)
    log(f"  moved {where}/{task_id} -> active/{task_id}")
    pipeline.process(task_from_dir(active_dir, lifecycle))
    return 0
