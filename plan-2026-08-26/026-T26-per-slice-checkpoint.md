# T26 — Checkpoint each slice so a crash resumes at the next one

**Wave 6** · depends: T21, T22 · finding: F8

## Context
`slices` is a single checkpointable stage (`CheckpointStage.SLICES`). `stage_slices` loops the parsed
slice list, and for each slice runs implement → review loops, all inside one stage. If the process
dies after slice 3 of 5 passes all reviews, `CheckpointStage.SLICES` was never appended, so a resume
re-runs slices 1–3: three full implement sessions plus their reviews, on models, for work that is
already committed on the task branch. That is the single largest waste in the loop and it is what
slice-level checkpointing exists to stop.

## Read first
- `harness/workflow/task_lifecycle.py` — `TaskState` (+ `checkpointed_stages`, and T22's `workdir`),
  `checkpoint()`, `_parse_stages` (the order + dedupe rules to mirror), `load_state`
- `harness/workflow/pipeline.py` — `stage_slices`, `_parse_slices` (`^### Slice <n>(.n)` from
  `artifacts/slices.md`), `_implement`, `_review_loop`, and where `checkpoint` is called per stage
- `harness/core/enums.py` — `CheckpointStage` (spec, feasibility, slicing, slices + CHECKPOINT_ORDER)
- `harness/workflow/params.py` — `StageContext`

## Do
1. Add `checkpointed_slices: list[str] = []` to `TaskState` and to `to_json`; `load_state` defaults a
   missing/`null` value to `[]` so old `task.json` files keep loading (an existing tested behaviour —
   do not break it).
2. Add `_parse_completed_slices(raw)` mirroring `_parse_stages`: drop non-strings with a warning,
   dedupe keeping first occurrence, **preserve insertion order** (slice completion order is the real
   order and `slices.md` order is not guaranteed to match execution order).
3. In `stage_slices`, before working a slice: `if sid in state.checkpointed_slices: log(skip)` and
   continue. After a slice passes **its last required review** (not merely after implement), append
   `sid` via a new `TaskLifecycle.checkpoint_slices(task_id, [sid])` that reuses `write_atomic` and
   the same read-modify-write discipline as `checkpoint()`.
4. Keep `CheckpointStage.SLICES` exactly as it is, as the stage-level marker appended when the whole
   loop finishes. **Do not overload it** with slice ids — a stage checkpoint answers "is this stage
   done", a slice checkpoint answers "which of these are done". Those are different questions and the
   resume decision uses both: stage done → skip stage; stage not done → skip the finished slices.
5. Slice ids are the strings `_parse_slices` yields (`"3"`, `"3.1"`, …) — store them as strings, never
   ints, and never re-format them (`3.10` must not become `3.1`).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json, tempfile, pathlib; sys.path.insert(0,'.')
from harness.workflow.task_lifecycle import TaskState, TaskLifecycle
s = TaskState(id="x", status="active", source="t", created="now")
assert s.checkpointed_slices == []
src = pathlib.Path('harness/workflow/pipeline.py').read_text()
assert 'checkpointed_slices' in src, "stage_slices does not consult slice checkpoints"
assert 'CheckpointStage.SLICES' in src, "stage-level marker was removed (must stay)"
import inspect
from harness.workflow import task_lifecycle as TL
f = TL.TaskLifecycle.checkpoint_slices
p = inspect.signature(f); assert list(p.parameters)[:2] == ['self','task_id']
# order + dedupe rules
for bad, want in ((["3","3","1"], ["3","1"]), (["3.10","3.1"], ["3.10","3.1"]), ([], [])):
    got = TL._parse_completed_slices(json.dumps(bad)) if isinstance(bad, list) else None
    assert (got if got is not None else bad) == want
print("slice checkpoint ok")
PY
python3 -m unittest discover -s tests    # the existing resume/checkpoint tests are the regression net
```
Must pass, plus the Gate.

## Out of scope
The merge checkpoint and branch-deletion change (T27 — it depends on this card), any change to
`_parse_slices`' parsing of `slices.md`, per-review (as opposed to per-slice) checkpointing, changing
`CHECKPOINT_ORDER`, rewriting the live `002` `task.json` to backfill slice ids (deferred by D4), and
any change to what a "passing" slice means.

## Done when
`task.json` gains `checkpointed_slices`; a task with `["1","2","3"]` skips those three and runs 4–5
(asserted by a unit test with a stub runner counting sessions); old-format `task.json` still loads;
`CheckpointStage.SLICES` is still appended when the loop completes.
