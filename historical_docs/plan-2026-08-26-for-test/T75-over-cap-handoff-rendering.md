# T75 — Render the over-cap handoff in the parked review file

**Depends:** T74 · **Leaf ticket** (second leaf of the re-sliced T50)

## Context
The persistence/rendering half of T50, kept separate per the algorithm's partition order (policy
before persistence). It changes how a parked over-cap task is *written down*, not when it parks.

## Read first
- harness/workflow/task_lifecycle.py
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_over_cap_handoff.py`.

Add a `Handoff` dataclass (stage, slice id, iteration, peak, cap, output path,
`checkpointed_stages`, `checkpointed_slices`) and an optional `handoff` parameter to
`TaskLifecycle.park()` that appends `## Handoff` (one line per field) and `## Next agent should`
("re-split the work or reduce its context before resume") to the review file; pass the caught
`OverContextBudget` fields from `Pipeline.process()`. A park without a handoff renders exactly what
it renders today.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_over_cap_handoff -v
```
Global Gate must pass.

## Out of scope
Raising `OverContextBudget`, the no-retry rule and the park decision (T74); stats rows (T49); cap
value; automatic resume; every other review-file section.

## Done when
A parked over-cap review file contains both sections with every field, a plain park is byte-identical
to today's output, the named tests prove it, the Gate passes, and no out-of-scope file changed.
