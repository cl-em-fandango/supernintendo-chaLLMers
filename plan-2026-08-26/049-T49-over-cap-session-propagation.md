# T49 — Propagate over-cap results into one stats row

**Depends:** T48 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/core/session.py

## Do
Create the new file: `tests/test_over_cap_session.py`.

Pass the configured cap to pi_cli; copy structured over-cap fields into SessionResult; annotate the same SessionRecord notes before its single append. Test one invocation produces exactly one annotated row.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_over_cap_session -v
```
Global Gate must pass.

## Out of scope
No process termination, retries, parking, or handoff rendering.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
