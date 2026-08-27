# T13 — Pure cycle decision: in-flight beats claims beats pending beats generate

**Wave 3** · depends: T09, T11 · finding: F1

## Context
`supervisor.py:198-209` picks the cycle's work from one number: `len(provider.fetch_pending())`.
`active/` and `claimed/` are never consulted. Right now `pending=0` while
`002-pipeline-checkpoint-and-resume` sits in `active/` and 7 files sit in `claimed/`, so every
cycle spawns `autonomous` generation instead of finishing work already started — the resume
feature (slices 1–3) is unreachable unattended.
The decision must be a pure function: `supervisor.py` executes `WORK_DIR = Path(load(CONFIG_PATH)...)`
at import time (l.38), so importing it from a test reads real config and touches real directories.

## Read first
- `supervisor.py` — `run_loop()` 149-215, work block 197-209, the import-time load at l.38
- `harness/workflow/continue_fresh.py` — `in_flight_task_dirs()` (what "in flight" means)
- `harness/core/providers.py` — `fetch_pending`, and T09's `list_claims()`
- `harness/workflow/params.py` — 11 lines: the import-clean module style to copy

## Do
1. New file `harness/workflow/cycle.py`. Imports: `enum` only. No `Config`, no `pathlib`, no
   `harness.core.config`, no `supervisor` — nothing that touches the filesystem at import.
2. `class CycleAction(str, Enum)`: `RESUME = "resume"`, `WORK = "work"`, `GENERATE = "generate"`.
3. `def decide_cycle_action(pending: int, in_flight: int, claims: int) -> CycleAction` — pure and
   total. First match wins: `in_flight > 0` → `RESUME`; `claims > 0` → `WORK`; `pending > 0` →
   `WORK`; else `GENERATE`. Any negative argument raises `ValueError`.
4. `def cycle_summary(pending, in_flight, claims, action) -> str` returning exactly
   `f"pending={pending} in_flight={in_flight} claimed={claims} action={action.value}"` (T14 logs it).
5. In `decide_cycle_action`'s docstring: the precedence table, and the reason claims count as
   *work* — stale claims are requeued by `cmd_run_task_loop` before the decision (T12), so what
   is left in `claimed/` is a task someone started and must finish.
6. Record the **D4 caveat** in that same docstring, as a known blocked state and not a bug here:
   T12's loop-start requeue ships **off by default**, so with the 7 live claims sitting put,
   `claims > 0` returns `WORK` forever and generation is blocked (T15's backoff bounds the cost).
   Two consequences: this function stays pure — three ints in, one action out, no policy, no
   thresholds — and it is the **caller** (T14) that decides which number to pass as `claims`.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, subprocess; sys.path.insert(0,'.')
from harness.workflow.cycle import CycleAction, decide_cycle_action as d, cycle_summary
assert d(0,1,0) is CycleAction.RESUME              # in-flight beats everything
assert d(5,1,2) is CycleAction.RESUME
assert d(0,0,1) is CycleAction.WORK                # a claim is work, not garbage
assert d(3,0,0) is CycleAction.WORK
assert d(0,0,0) is CycleAction.GENERATE
try:
    d(-1,0,0); raise AssertionError("negative accepted")
except ValueError:
    pass
assert cycle_summary(1,2,3,CycleAction.RESUME) == "pending=1 in_flight=2 claimed=3 action=resume"
rc = subprocess.run([sys.executable,"-c",
    "import sys;sys.path.insert(0,'.');import harness.workflow.cycle;"
    "assert 'harness.core.config' not in sys.modules;"
    "assert 'supervisor' not in sys.modules;print('import clean ok')"],
    capture_output=True, text=True)
assert rc.returncode == 0, rc.stderr
print("cycle decision ok")
PY
```
Must pass, plus the Gate.

## Out of scope
Wiring the decision into the supervisor and the `run-task-loop --continue` spawn (T14),
no-progress backoff (T15), docstrings (T16), stale-claim requeue policy (T12 owns it), any change
to `cmd_run*` or to `supervisor.py` at all.

## Done when
`harness/workflow/cycle.py` exists and imports only `enum`; the truth table above passes as
written; `git status --short` shows exactly one new untracked file (the card's own commit adds
`cycle.py` only).
