# T60 — Implement pure read-only queue inventory

**Depends:** T21, T22, T23, T51 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/core/providers.py
- harness/workflow/task_lifecycle.py
- harness/core/config.py

## Do
Create the new files: `harness/workflow/queue_audit.py`, `tests/test_queue_audit.py`.

Return inventory/anomaly lines without writes. Cover status mismatch, old/missing state, queue git, session outputs, duplicate slugs, ownership metadata and short bodies. Test tree hash unchanged.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_queue_audit -v
```
Global Gate must pass.

## Out of scope
No CLI, report file, suggestions, or real queue.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
