# T14 — Supervisor drives each cycle from `decide_cycle_action` and runs `run-task-loop --continue`

**Wave 3** · depends: T13 · `[tag]` · finding: F1

## Context
`supervisor.py:197-209` is `if pending: run-one / else: autonomous`. `run-one` (handlers.py:68-83)
claims *all* pending files, processes `[0]`, requeues the rest, and neither branch ever resumes the
task already sitting in `active/`. With the live task `002-pipeline-checkpoint-and-resume` the
supervisor generates new work forever instead of finishing it, and `--continue` stays dead code in
the self-driving loop. T13 supplies the pure decision; this card wires it in.

## Read first
- `supervisor.py` — `run_loop()` 149-215 (replace 197-209), imports 34-35, module docstring 3-19
- `harness/workflow/cycle.py` — T13's `CycleAction`, `decide_cycle_action`, `cycle_summary`
- `harness/workflow/continue_fresh.py` — `in_flight_task_dirs(lifecycle)`
- `harness/cli/handlers.py` — `cmd_run_task_loop` 86-105 (the child being spawned)

## Do
1. In `run_loop()`, before `while not stop["flag"]`, build once: `cfg = load(CONFIG_PATH)`,
   `provider = create_provider(cfg)`, `lifecycle = TaskLifecycle(cfg, log=log)`. Today they are
   rebuilt inside the work block every cycle, which is why only one count is cheap enough to log.
2. Replace the work block (197-209) with: read-only counts `pending = len(provider.fetch_pending(claim=False))`,
   `in_flight = len(in_flight_task_dirs(lifecycle))`, `claims = len(provider.list_claims())`;
   `action = decide_cycle_action(pending, in_flight, claims)`; and a single log line
   `f"── cycle {cycle}: {cycle_summary(pending, in_flight, claims, action)} ──"`.
   Pass `claim=False` **explicitly** — a counting call must not be one default-flip away from moving
   the queue (the same lesson as `AutonomousGenerator._pending_count`, F14/T41; `count_pending()`
   replaces it once T41 lands). On `claims`: pass the total, and if the D4 state is what the
   `WORK` branch is chasing (nothing pending, nothing in flight, only stale claims) log one extra
   line naming it, so the block is visible instead of mysterious.
3. Spawn mapping: `RESUME` and `WORK` both spawn
   `[sys.executable, "harness.py", "run-task-loop", "--continue"]` (that command resumes `active/`
   first, then drains `pending/` one task at a time); `GENERATE` spawns
   `[sys.executable, "harness.py", "autonomous"]`. Log a non-zero child rc as today.
4. Do **not** touch the breaker block (178-195) — T06 owns it. Keep the per-cycle
   `harness.py status` probe and `_sleep(stop, SLEEP_S)`.
5. Module docstring: one cycle = status probe → decide → one `run-task-loop --continue` (or
   `autonomous`) → sleep. Drop the "Claims ONE task per cycle" bullet if it no longer describes the
   spawn; T16 finishes the docstring pass.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import pathlib, sys; sys.path.insert(0,'.')
src = pathlib.Path("supervisor.py").read_text()
assert "run-task-loop" in src and '"--continue"' in src, "no run-task-loop --continue spawn"
assert '"run-one"' not in src, "the old run-one spawn is still there"
assert '"autonomous"' in src, "generate branch missing"
assert "decide_cycle_action" in src and "cycle_summary" in src, "cycle decision not wired"
assert src.count("create_provider(") == 1, "provider still rebuilt per cycle"
assert "in_flight_task_dirs(" in src, "active/ never counted"
from harness.workflow.cycle import decide_cycle_action as d, CycleAction
assert d(0,1,0) is CycleAction.RESUME and d(0,0,0) is CycleAction.GENERATE
print("supervisor wiring ok")
PY
grep -c "cycle {cycle}" supervisor.py      # must print 1 — one summary line per cycle
```
Must pass, plus the Gate. **Never run `supervisor.py run` or `start` to verify this card.**

## Out of scope
Backing off when a cycle accomplishes nothing (T15), the breaker's inline `git reset --hard` (T06),
child output capture (T08), `cmd_run_task_loop`'s internals (shaped by T10/T12), starting an actual
supervised run, `claimed/` requeue policy.

## Done when
`supervisor.py` contains no `run-one` spawn; each cycle logs exactly one
`pending=/in_flight=/claimed=/action=` line; `RESUME` and `WORK` both spawn
`run-task-loop --continue`; Gate passes; `git tag -f pi/last-good pi/trunk` was run.
