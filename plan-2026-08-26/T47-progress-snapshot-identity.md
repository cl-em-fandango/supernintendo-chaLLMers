# T47 — Supervisor progress detection must compare task identity

**Wave 3** · depends: T15 · finding: hardening review F6

## Context
T15 compares only `(pending, in_flight, claims)` counts before and after a child. Different tasks can replace one another while all three counts remain equal, producing a false no-progress backoff.

## Read first
- `supervisor.py` — before/after progress check
- `harness/workflow/cycle.py`
- provider claim/list APIs and `in_flight_task_dirs`

## Do
1. Add immutable `CycleSnapshot` in `harness/workflow/cycle.py` with sorted tuples of pending ids, in-flight ids, and claimed ids.
2. Add pure `made_progress(before, after) -> bool` returning `before != after`.
3. Build snapshots in `supervisor.py` from the same read-only scans already used for counts. Derive logged counts from snapshot lengths so counts and identity cannot disagree.
4. Preserve T15 backoff arithmetic and interruptible sleep unchanged.
5. Add tests proving equal counts with changed task ids is progress and identical identities is not.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_cycle_decision -v
python3 - <<'PY'
from harness.workflow.cycle import CycleSnapshot, made_progress
x = CycleSnapshot(('a',), (), ())
y = CycleSnapshot(('b',), (), ())
assert made_progress(x, y)
assert not made_progress(x, x)
print('identity progress ok')
PY
```
Gate must pass. Never run the supervisor loop.

## Out of scope
Content hashing, task status semantics, backoff values, spawning/forking tests, queue mutation.

## Done when
Task replacement with unchanged counts resets the idle streak; an identical task snapshot increments it.
