# Refactor Chunk 5: Split pipeline.py into workflow/

## Context
CODING_STANDARDS.md §1 (one responsibility per file) and §2 (state/behavior
split). `harness/pipeline.py` (354 lines) mixes three jobs:
1. **Task lifecycle** — intake/park/fail/complete/exec-summary (moving task
   dirs between queue folders, writing review summaries).
2. **Stage orchestration** — the spec→feasibility→slicing→slices→holistic loop.
3. **Helpers** — `_parse_slices`, `_summary`, `_now`, `_json`.

This chunk splits it into a `workflow/` subpackage with one responsibility per
file, and introduces a named `StageContext` to replace the positional
`(tid, td, workdir)` tuples (CODING_STANDARDS §2/§5).

## Read first
- `CODING_STANDARDS.md` — §1, §2, §5
- `harness/pipeline.py` — the whole file
- `harness/autonomous.py` — will move into workflow/ too

## Do

Create `harness/workflow/` with `__init__.py`.

### 5a. `harness/workflow/params.py` — named state
```python
"""Named state objects for the pipeline (CODING_STANDARDS §2/§5)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StageContext:
    """Everything a stage needs, named instead of positional path tuples."""
    task_id: str
    task_dir: Path      # the active/<task_id> dir
    workdir: Path       # where the git repo / code lives
```

### 5b. `harness/workflow/task_lifecycle.py` — state transitions
Move these out of `Pipeline` into a `TaskLifecycle` class (or module
functions taking `cfg` + `log`). It owns the queue-folder moves and review
summaries:
- `task_dir(task_id, where="active") -> Path`
- `intake(task) -> Path`
- `park(task_id, reason) -> None`
- `fail(task_id, reason) -> None`
- `complete(task_id, summary) -> None`
- `_exec_summary(task_id, status, text, where) -> None`
- `resolve_workdir(td) -> Path`

It needs `cfg` (for `queue_dir`, `trunk_branch`) and a `log` callable. Give it
`__init__(self, cfg, log=print)`. Use the `TaskStatus` enum from
`harness.core.enums` for the status strings where natural (e.g. the
`task.json` `"status"` field and the exec-summary status), but keep the on-disk
string values identical (`"active"`, `"PARKED"`, etc.) so existing task dirs
and the review format don't change.

### 5c. `harness/workflow/pipeline.py` — orchestration only
`Pipeline` keeps:
- `__init__(cfg, runner, log, provider)` — now also builds a
  `TaskLifecycle(cfg, log)` and stores it as `self.lifecycle`.
- `_run(...)` (the crash-retry wrapper) — unchanged.
- `process(task)` — unchanged logic, but calls `self.lifecycle.intake/park/
  fail/complete/resolve_workdir` instead of its own copies.
- `stage_spec`, `stage_feasibility`, `stage_slicing`, `stage_slices`,
  `_implement`, `_review_loop`, `stage_holistic` — unchanged logic.

**Introduce `StageContext`:** the stage methods currently take
`(self, tid, td, workdir)`. Change them to take `(self, ctx: StageContext)` and
read `ctx.task_id`, `ctx.task_dir`, `ctx.workdir` inside. `process()` builds
one `StageContext(task.id, td, workdir)` after intake and passes it to each
stage. This is the §2/§5 win: no more positional path tuples.

Move the module-level helpers `_parse_slices`, `_summary`, `_now`, `_json`
into `workflow/pipeline.py` (they are orchestration helpers) — or into a small
`workflow/_util.py` if you prefer. Keep `_now`/`_json` available to
`task_lifecycle.py` (import from wherever they land).

### 5d. Move `harness/autonomous.py` → `harness/workflow/autonomous.py`
Pure move. Update its internal imports (`.config` → `.core.config`,
`.providers` → `.core.providers`, `.session` → `.core.session`,
`.prompts` → `.core.prompts`).

### 5e. Update importers
- `harness.py`: `from harness.pipeline import Pipeline` →
  `from harness.workflow.pipeline import Pipeline`;
  `from harness.autonomous import AutonomousGenerator` →
  `from harness.workflow.autonomous import AutonomousGenerator`.

## Rules
- No behavior change: same stages, same order, same verdict handling, same
  crash-retry, same queue moves, same review-summary format.
- `workflow/` may import from `core/` and `external/`, never from `cli/`.
- The on-disk task.json and review/*.md formats are byte-identical to before.

## Verify (the gate)
```
cd /home/donald/work/harness
# old grab-bag is gone, workflow/ exists
! test -e harness/pipeline.py && ! test -e harness/autonomous.py && echo "moved ✓"
ls harness/workflow/*.py
# no positional (tid, td, workdir) left in stage signatures
! grep -qE "def stage_\(self, tid: str, td: Path, workdir: Path\)" harness/workflow/pipeline.py && echo "StageContext in use ✓"
# full gate
python3 -c "import sys; sys.path.insert(0,'.'); import harness, harness.workflow.pipeline, harness.workflow.task_lifecycle, harness.workflow.autonomous, harness.workflow.params; print('import ok')"
python3 harness.py status
python3 harness.py report >/dev/null && echo "report ok"
```
All must pass.

## Commit
```
git add -A
git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "harness: split pipeline into workflow/ (lifecycle, params, orchestration)"
```
Then: `git tag -f pi/last-good pi/trunk`

## Done when
- `harness/workflow/` has `params.py`, `task_lifecycle.py`, `pipeline.py`,
  `autonomous.py`
- `harness/pipeline.py` and `harness/autonomous.py` no longer exist at root
- Stage methods take `StageContext`, not positional path tuples
- Gate passes (import + status + report)
- Committed and `pi/last-good` advanced
