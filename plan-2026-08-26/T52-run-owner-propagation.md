# T52 — Propagate one owner id through each run command

**Depends:** T10, T51 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below. Its suite is named for
the behavior (`tests/test_run_owner_id.py`) so no sibling leaf shares a file with it — T61 also
claimed `tests/test_handlers.py`, which made the two leaves un-revertible apart.

## Read first
- harness/cli/handlers.py

## Do
Create the new file: `tests/test_run_owner_id.py`.

Generate one owner id per command invocation; pass it to claim and finally-cleanup calls; prove one invocation cannot clean another owner’s claim.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_run_owner_id -v
```
Global Gate must pass.

## Out of scope
No stale reclaim, operator force, provider schema changes, or parser changes.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
