# T44 — Represent a claimed-only queue as blocked, not actionable work

**Wave 3** · depends: T12, T13, T14 · finding: hardening review F5

## Context
With automatic stale reclaim disabled by D4, `pending=0`, `in_flight=0`, `claims>0` currently maps to `WORK`. The supervisor then spawns `run-task-loop --continue`, which cannot consume those claims. Calling this state work causes an endless sequence of no-op children.

## Read first
- `harness/workflow/cycle.py`
- `supervisor.py` — cycle decision and spawn mapping
- `harness/cli/handlers.py` — `cmd_run_task_loop`, stale-claim option
- `plan-2026-08-26/T12-stale-claim-requeue.md`

## Do
1. Add `CycleAction.BLOCKED = "blocked"`.
2. Decision order is: in-flight → `RESUME`; pending → `WORK`; claimed-only → `BLOCKED`; empty → `GENERATE`.
3. `BLOCKED` must spawn no child. Log one operator-action line naming `harness.py requeue-claims --dry-run` and the number of claims.
4. Sleep through the existing backoff path; do not fail, move, or requeue a claim.
5. Update `cycle_summary` and permanent cycle tests for the exact claimed-only case.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_cycle_decision -v
python3 - <<'PY'
import sys
sys.path.insert(0, '.')
from harness.workflow.cycle import CycleAction, decide_cycle_action
assert decide_cycle_action(0, 0, 2) is CycleAction.BLOCKED
assert decide_cycle_action(1, 0, 2) is CycleAction.WORK
assert decide_cycle_action(0, 1, 2) is CycleAction.RESUME
print('claimed-only block ok')
PY
```
Gate must pass. Never run the supervisor loop.

## Out of scope
Requeue policy, claim ownership/locking, changing D4, autonomous generation while claims exist, supervisor process management.

## Done when
A claimed-only queue is visible as `action=blocked`, launches no harness child, and remains responsive through the existing interruptible backoff.
