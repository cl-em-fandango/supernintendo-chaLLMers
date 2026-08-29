# T60 — Implement pure read-only queue inventory (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** Execute T76 then T77. This file is retained only as the
> parent contract.

**Depends:** T21, T22, T23, T51 · **Re-sliced into T76 → T77**

## Context
Re-sliced on 2026-08-26 (see `plan-2026-08-26-done/SLICING-MAP.md`): its `Do` listed seven unrelated anomaly classes in
one ticket, over the five-criterion ceiling, and `fits()` Q8 applies — one check could be implemented
while another silently regressed. T76 owns the inventory, the task-state anomalies and `.git`
detection; T77 owns the stray-output, duplicate-slug, claim-metadata and short-body anomalies plus
the operator footer. Together they own every criterion below exactly once.

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
