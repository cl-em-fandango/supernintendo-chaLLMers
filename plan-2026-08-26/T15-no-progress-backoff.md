# T15 — A cycle that accomplished nothing must back off, not re-probe every 60 s forever

**Wave 3** · depends: T14 · finding: F13 (spin class), F1

## Context
Be precise about the bug: `_sleep()` (supervisor.py:219-222) **is** a real 1 Hz interruptible
sleep, so nothing spins today — the historical `[DRY] run-task-loop --continue` spin storm in the
tail of the 179 MB `supervisor.log` came from the deleted `supervisor.sh`. The live defect is
different: after T14 a cycle that accomplishes nothing still costs a `harness.py status` probe, a
`run-task-loop`/`autonomous` spawn (which re-reads config and re-scans the queue), and log lines —
forever, every `SLEEP_S`. A wedged task or an unreachable model endpoint burns CPU, disk and log
volume with zero progress and no signal to a human.

## Read first
- `supervisor.py` — `run_loop()` work block (post-T14), `_sleep()` 219-222, constants 43-45
- `harness/workflow/cycle.py` — T13's module (pure, import-clean: `supervisor.py` loads real
  config at import, so the backoff math must not live there)

## Do
1. Add to `harness/workflow/cycle.py`: `def backoff_seconds(idle_streak: int, base: int, cap: int) -> int`
   — `min(base * 2**idle_streak, cap)`, `idle_streak < 0` raises `ValueError`, returns `base` when
   `idle_streak == 0` (today's behavior at streak 0, so a healthy loop is unchanged).
2. `supervisor.py`: new constant `MAX_SLEEP_S = int(os.environ.get("SUPERVISOR_MAX_SLEEP_S", "900"))`.
3. In `run_loop()`: keep the pre-spawn counts from T14 as `before`; after the child exits, re-read
   the same three counts as `after`. `progressed = after != before` — a state change (claim→active,
   active→done/parked, new pending from generation) is progress; identical counts are not.
4. `idle_streak = 0 if progressed else idle_streak + 1` (initialise `idle_streak = 0` beside
   `failcount = 0`). When `idle_streak >= 1`, log
   `f"  no progress (streak {idle_streak}); sleeping {secs}s"` and sleep `secs` from
   `backoff_seconds(idle_streak, SLEEP_S, MAX_SLEEP_S)`; otherwise sleep `SLEEP_S` as now.
5. The sleep must still go through `_sleep(stop, secs)` — never `time.sleep(secs)` — so SIGTERM and
   the STOP file stay responsive during backoff. Leave the breaker's `_sleep(stop, SLEEP_S)` alone.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import pathlib, sys; sys.path.insert(0,'.')
from harness.workflow.cycle import backoff_seconds as b
assert b(0, 60, 900) == 60 and b(1, 60, 900) == 120 and b(2, 60, 900) == 240
assert b(10, 60, 900) == 900, "cap not applied"
try:
    b(-1, 60, 900); raise AssertionError("negative streak accepted")
except ValueError:
    pass
src = pathlib.Path("supervisor.py").read_text()
assert "backoff_seconds(" in src, "backoff not wired"
assert "MAX_SLEEP_S" in src, "cap constant missing"
assert "idle_streak" in src, "no idle tracking"
assert "time.sleep(" not in src.split("def _sleep")[0], "raw sleep in the loop body"
print("backoff ok")
PY
grep -n "_sleep(stop" supervisor.py        # every sleep in the loop goes through _sleep
```
Must pass, plus the Gate. Do not start the supervisor to verify.

## Out of scope
Rotating/capping `supervisor.log` (T2), the breaker's reset and its sleep (T6), marking a task
failed after N idle cycles (needs a human policy decision — hand over instead), `harness.log`
rotation (T7), any change to `_sleep`'s own implementation.

## Done when
`backoff_seconds` passes the table above; a cycle with unchanged queue counts sleeps twice the
previous idle sleep, capped at `MAX_SLEEP_S`, and logs the streak; a cycle that changes state
sleeps exactly `SLEEP_S`; SIGTERM still stops the loop mid-backoff (read `_sleep`, confirm).
