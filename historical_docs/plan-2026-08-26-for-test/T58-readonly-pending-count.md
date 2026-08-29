# T58 — Give autonomous generation a read-only pending count

**Depends:** T09 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/core/providers.py
- harness/workflow/autonomous.py

## Do
Create the new file: `tests/test_autonomous_count.py`.

Add count_pending and use it from autonomous pending count. Prove pending and claimed directories are byte-identical after counting.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_autonomous_count -v
```
Global Gate must pass.

## Out of scope
No generation policy, claims, handlers, or queue mutation.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
