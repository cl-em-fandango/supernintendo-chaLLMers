# T38 — Supervisor cycle-decision tests, importing only the pure module

**Wave 9** · depends: T13, T14, T15 · finding: F11

## Context
`supervisor.py` is 301 lines covering the lock, the daemon fork, the circuit breaker and the cycle
loop, and it is untested (F11). It **cannot** be imported by a test: `WORK_DIR = Path(load(CONFIG_PATH)…)`
runs at import time (l.38), so an import reads the real `config.json` and creates real directories —
which is precisely why T13 extracted the decision into `harness/workflow/cycle.py`. This card tests
the pure module and the breaker *arithmetic*, never the loop, never a spawn, never a fork.

## Read first
- `harness/workflow/cycle.py` — `CycleAction`, `decide_cycle_action`, `cycle_summary` (T13)
- `supervisor.py` — `run_loop()`'s breaker block (`failcount`, `FAIL_LIMIT`, the `_sleep` +
  `continue`), and the work block T14 wired — **read it, do not import it**
- `plan-2026-08-26/T13-cycle-decision-function.md`, `T15-no-progress-backoff.md` — the tables to pin
- `tests/test_pipeline_resume.py` — house style for stub objects

## Do
1. New file `tests/test_cycle_decision.py`. First line of the module docstring: **"this test must
   never import `supervisor`"** — and add a test that asserts it: run a `subprocess` that imports
   `harness.workflow.cycle` and asserts `'supervisor' not in sys.modules` and
   `'harness.core.config' not in sys.modules` (the T13 guard, promoted to CI).
2. Cases for `decide_cycle_action`: in-flight beats pending; pending produces `WORK`; after T44,
   claimed-only produces `BLOCKED`; all-zero → `GENERATE`; negatives → `ValueError`; a large-count
   sanity case; and `cycle_summary`'s exact string.
3. Test T14's pure `command_for_action`: `RESUME` and `WORK` map exactly to
   `harness.py run-task-loop --continue`, `GENERATE` maps exactly to `harness.py autonomous`, and
   `BLOCKED` maps to no command after T44. Also parse `supervisor.py` with `ast` and assert its spawn
   argument is obtained from `command_for_action`, not from duplicated command literals. This proves
   the decision-to-command wiring without importing or executing the supervisor.
4. Replicate the **breaker arithmetic** as a pure helper test: if T14/T15 left the counting inside
   `run_loop`, do not copy the loop into the test — instead assert the *contract* by reading the
   source (`ast`) for: `FAIL_LIMIT` compared before the reset happens, and `_sleep` on every failure
   path. If the arithmetic is not extractable without changing `supervisor.py`, add a small pure
   function `next_fail_state(failcount, rc, limit) -> (new_count, should_reset)` to
   `harness/workflow/cycle.py` (that module is allowed to grow) and test it — do **not** edit
   `supervisor.py` in this card.
5. Cases for `next_fail_state` if you add it: rc 0 resets to 0; rc≠0 increments; the increment that
   reaches the limit is the one that returns `should_reset True`; after a reset the count is 0.
6. Backoff (T15): assert the *shape* — a pure `backoff_seconds(consecutive_no_progress, base, cap)`
   (in `cycle.py`) is monotonic non-decreasing, capped, and returns `base` on the first no-progress
   cycle. If T15 already has such a helper elsewhere, test that one instead of adding a duplicate.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, subprocess, unittest, pathlib; sys.path.insert(0,'.')
p = pathlib.Path('tests/test_cycle_decision.py'); assert p.exists()
assert 'import supervisor' not in p.read_text(), "test imports supervisor (forbidden)"
rc = subprocess.run([sys.executable,"-c",
  "import sys;sys.path.insert(0,'.');"
  "import harness.workflow.cycle as c;"
  "assert 'supervisor' not in sys.modules;"
  "assert 'harness.core.config' not in sys.modules;print('clean')"],
  capture_output=True, text=True)
assert rc.returncode == 0, rc.stderr
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_cycle_decision')
assert suite.countTestCases() >= 8, f"only {suite.countTestCases()} cases"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
print(f"cycle decision tests ok ({suite.countTestCases()} cases)")
PY
```
Must pass, plus the Gate.

## Out of scope
Anything that spawns a process, forks, writes `supervisor.log`, touches `STOPFILE`/`PIDFILE`, or calls
`daemonize`/`kill_tree` — no test in this card may execute `supervisor.py`. Also out: the real queue
counts (the live `pending=0 / active=1 / claimed=7` state is an audit input, T25, not a fixture), the
git breaker command itself (T06, T36), and changing `supervisor.py`'s wiring (T14 owns it).

## Done when
`tests/test_cycle_decision.py` has ≥8 green cases; it does not import `supervisor` anywhere and
proves the pure module stays import-free of `supervisor` and `harness.core.config`; no test writes a
file outside a temp dir.
