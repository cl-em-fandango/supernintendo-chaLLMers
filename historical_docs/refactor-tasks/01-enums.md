# Refactor Chunk 1: Add shared enums

## Context
The harness uses bare strings for discrete state: task status
(`"active"`, `"parked"`, `"done"`, `"failed"`) and session verdicts
(`"pass"`, `"fail"`, `"kickback"`, `"done"`, `"progress"`, `"resliced"`,
`"infeasible"`, `"rejected"`). CODING_STANDARDS.md §3 requires enums for
discrete state. This chunk ADDS the enums only — it changes no call sites, so
it is zero-risk and purely additive.

## Read first
- `CODING_STANDARDS.md` (repo root) — §3 "Enums instead of magic strings"
- `harness/providers.py` — where task status strings are used
- `harness/session.py` — where verdict strings are parsed/returned
- `harness/pipeline.py` — where verdicts are compared

## Do
Create `harness/enums.py` with three enums. Use `str, Enum` so members
compare equal to their string value (this keeps existing string comparisons
working during the transition, and lets us serialize to JSON/git refs without
`.value` everywhere yet):

```python
"""Shared enums for discrete state. See CODING_STANDARDS.md §3."""
from __future__ import annotations
from enum import Enum


class TaskStatus(str, Enum):
    """Where a task lives in the queue lifecycle."""
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    PARKED = "parked"
    FAILED = "failed"


class Verdict(str, Enum):
    """The VERDICT a session emits. Values match the strings in prompts.py."""
    PASS = "pass"
    FAIL = "fail"
    KICKBACK = "kickback"
    DONE = "done"
    PROGRESS = "progress"
    RESLICED = "resliced"
    INFEASIBLE = "infeasible"
    REJECTED = "rejected"


class Stage(str, Enum):
    """Pipeline stage names, used in stats and logs."""
    SPEC = "spec_author"
    FEASIBILITY = "feasibility"
    SLICING = "slicing"
    SLICE_FIT = "slice_fit"
    IMPLEMENT = "implement"
    TECH_REVIEW = "tech_review"
    FUNC_REVIEW = "func_review"
    FIX_TECH = "fix_tech"
    FIX_FUNC = "fix_func"
    HOLISTIC = "holistic_review"
    AUTONOMOUS_SUGGEST = "autonomous_suggest"
    AUTONOMOUS_REVIEW = "autonomous_review"
```

Do NOT change any other file in this chunk.

## Verify (the gate)
```
cd /home/donald/work/harness
python3 -c "import sys; sys.path.insert(0,'.'); from harness.enums import TaskStatus, Verdict, Stage; print(TaskStatus.PARKED, Verdict.PASS, Stage.SPEC)"
python3 harness.py status
```
Both must succeed. Also confirm string equality holds (transition safety):
```
python3 -c "from harness.enums import Verdict; assert Verdict.PASS == 'pass'; print('ok')"
```

## Commit
```
git add -A
git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "harness: add shared enums (TaskStatus, Verdict, Stage)"
```
Then advance the tag: `git tag -f pi/last-good pi/trunk`

## Done when
- `harness/enums.py` exists with the three enums
- Gate passes (import + status)
- `Verdict.PASS == "pass"` is True
- No other file changed
- Committed and `pi/last-good` advanced
