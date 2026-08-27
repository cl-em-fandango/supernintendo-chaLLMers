# T50 — Park an over-cap session with structured handoff

**Depends:** T26, T49 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/workflow/pipeline.py
- harness/workflow/task_lifecycle.py

## Do
Create the new file: `tests/test_over_cap_handoff.py`.

Raise a structured OverContextBudget before retry/verdict routing; catch once in process; park with stage, slice, iteration, peak, limit, output path and checkpoint lists; render Handoff and Next-agent sections. Prove no retry.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_over_cap_handoff -v
```
Global Gate must pass.

## Out of scope
No subprocess parsing, stats writes, cap changes, or automatic resume.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
