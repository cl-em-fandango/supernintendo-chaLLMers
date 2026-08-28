# T50 — Park an over-cap session with structured handoff (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** Execute T74 then T75. This file is retained only as the
> parent contract.

**Depends:** T26, T49 · **Re-sliced into T74 → T75**

## Context
Re-sliced on 2026-08-26 (see `plan-2026-08-26-done/SLICING-MAP.md`): this leaf crossed workflow routing and markdown
persistence/rendering in one ticket (`fits()` Q1) and touched two production modules plus a test
module, so `RECURSIVE-SLICING-ALGORITHM.md` forces another split. T74 owns the routing and the park,
T75 owns the handoff rendering; together they own every criterion below exactly once.

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
