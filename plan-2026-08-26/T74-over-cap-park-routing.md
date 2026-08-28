# T74 — Park an over-cap session without retrying it

**Depends:** T26, T49 · **Leaf ticket** (first leaf of the re-sliced T50)

## Context
T50 grew past the hard limits in `RECURSIVE-SLICING-ALGORITHM.md`: it crossed workflow routing and
markdown persistence/rendering in one ticket (`fits()` Q1) and touched two production modules plus a
test module. This leaf owns only the routing decision — the park. The handoff markdown is T75.

## Read first
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_over_cap_park.py`.

Add `OverContextBudget` carrying task id, stage, slice id, iteration, peak, limit and `out_file`;
check `SessionResult.over_context_budget` in `Pipeline._run` **before** the crash-retry branch and
raise it; catch it once in `Pipeline.process()` and park with the reason
`over context budget: peak=<n> limit=<n>`. An over-cap result is never retried and its partial
verdict is never routed on.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_over_cap_park -v
```
Global Gate must pass.

## Out of scope
The `## Handoff` / `## Next agent should` sections and any `TaskLifecycle.park()` signature change
(T75); subprocess termination (T48); the stats annotation (T49); crash-retry exhaustion (T57);
unpark or automatic resume.

## Done when
One stubbed over-cap result produces exactly one runner call, exactly one park and no verdict
routing, the named test proves it, the Gate passes, and no out-of-scope file changed.
