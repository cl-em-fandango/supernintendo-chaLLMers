# T66 — Test status and explicit claim reclaim handlers

**Depends:** T11, T12, T53 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/cli/handlers.py
- harness/core/providers.py

## Do
Create the new file: `tests/test_handlers_claims.py`.

Test claimed status row/ages, dry-run, empty reclaim, ownership-aware force, and filename/slug matching in temp dirs.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_handlers_claims -v
```
Global Gate must pass.

## Out of scope
No run loops, parser surface, autonomous, or real queue.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
