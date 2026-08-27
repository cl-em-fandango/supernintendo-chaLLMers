# T41 — Five small F14 fixes, each verified on its own

**Wave 10** · depends: T29, T30, T31 · finding: F14

## Context
Five independent low-severity defects, each one or two lines wide, each with a distinct owner-less
fix. They are grouped because none justifies a session of its own and all are mechanical. Working
order is the order below — do them one at a time with its verify line, not as one sweep, so a failure
isolates to one change.

## Read first
- `harness/cli/handlers.py` + `harness/cli/parser.py` — `cmd_resume`, `run-task`'s `--fresh` flag,
  `harness/workflow/continue_fresh.py:fresh_restart`
- `harness/workflow/pipeline.py` — `_review_loop` (model choice + the progress-note write), `_run`
  (the retry loop and its return), and the two fix call sites
- `harness/workflow/autonomous.py` — `_pending_count()`

## Do
1. **`resume --fresh`** — add the flag to the `resume` subparser and pass it through
   `resume_task(..., fresh=False)` to the existing `fresh_restart(task_id, cfg, log)`. Do not
   reimplement: `run-task --fresh` already works, and `cmd_unpark` must keep its current
   checkpoint-preserving behaviour (EC12: `resume --fresh` drops checkpoints, `unpark` does not).
2. **Functional fixes run on the implementer model** — `_review_loop` picks
   `self.cfg.implementer if kind == "tech" else self.cfg.model`, so a *functional-review fix* is
   written by the technical writer. Both fix paths are code edits, so both take
   `self.cfg.implementer`. Say *why* in the commit message: the model choice followed the review type
   instead of the work type.
3. **Progress-note collision** — review feedback and the implementer's progress note are both written
   to `artifacts/progress/slice-{sid}.md`, so each overwrites the other. Keep the implementer path;
   write review feedback to `artifacts/progress/slice-{sid}-review.md`. Check nothing reads the old
   path for review text (`grep -rn "progress/slice" harness/`) before moving it.
4. **`_run` must not return a wreck as if it were a result** — today, after `max_crash_retries + 1`
   crashed attempts it returns the last result and callers only look at `verdict`. **Choose the
   exception, not a field**: raise `AllAttemptsCrashed(task_id, stage, attempts)` from `_run` and
   handle it in `process()` as a park with the attempt count in the reason. Record the choice and why
   (a field is forgettable at a future call site; an exception is not) in the commit message.
5. **`_pending_count` must not be able to claim** — replace
   `AutonomousGenerator._pending_count()`'s `provider.fetch_pending()` with a read-only
   `provider.count_pending()` on `TaskProvider`/`DirectoryTaskProvider` that lists and returns an
   `int` without touching the filesystem state. It is safe today only because `claim` defaults to
   `False`; a flipped default would silently move every pending task into `claimed/` from inside a
   *counter*.

## Verify
Run all five; each must print its own `ok`.
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, inspect, pathlib; sys.path.insert(0,'.')
h = pathlib.Path('harness/cli/handlers.py').read_text(); p = pathlib.Path('harness/cli/parser.py').read_text()
assert 'fresh' in inspect.signature(__import__('harness.workflow.resume', fromlist=['resume_task']
        ).resume_task).parameters, "resume_task has no fresh parameter"
assert p.count('--fresh') >= 2, "--fresh still only on run-task"
pl = pathlib.Path('harness/workflow/pipeline.py').read_text()
assert 'slice-{sid}-review' in pl or 'slice-{" + ' in pl, "review note path not split"
assert 'AllAttemptsCrashed' in pl and 'self.cfg.model' not in pl.split('_review_loop')[1][:1200]
from harness.core.providers import DirectoryTaskProvider, TaskProvider
assert hasattr(TaskProvider, 'count_pending') and hasattr(DirectoryTaskProvider, 'count_pending')
au = pathlib.Path('harness/workflow/autonomous.py').read_text()
assert 'fetch_pending' not in au.split('def _pending_count')[1].split('\n    def ')[0], "counter can still claim"
import tempfile, os
root = pathlib.Path(tempfile.mkdtemp())
for d in ("pending","claimed"): (root/d).mkdir()
for i in range(3): (root/"pending"/f"{i}-t.md").write_text("x")
prov = DirectoryTaskProvider(root/"pending", root/"claimed")
assert prov.count_pending() == 3 and list((root/"claimed").iterdir()) == [], "count_pending mutated the queue"
print("all five small fixes ok")
PY
```
Must pass, plus the Gate.

## Out of scope
The `interrupt` / stand-down command (parked task `001` asks for it — **D6: explicitly out of this
plan**), anything in `_outcome`/verdict mapping (T20), the over-cap trip (T42), splitting `slice_fix`
into per-kind stage values (T30 says no), and the queue contents themselves (D4).

## Done when
Each of the five verify assertions passes; `resume --fresh` restarts a task and `unpark` still
preserves checkpoints (assert both in one new test each); `grep -n "fetch_pending" harness/workflow/autonomous.py`
is empty; the commit message states the two judgement calls (exception-vs-field; implementer-for-both-fix-kinds).
