# Phase 1: Core Architecture, Workflow & Lifecycle State Review

## Mission
Audit all pipeline stage routing, checkpointing, session resume logic, state transitions, and enum migrations against the defined specifications.

## Historical Reference Documents
- `historical_docs/AUDIT-2026-08-26.md` (Findings: F4 status not updated)
- `historical_docs/HARDENING-ANALYSIS-2026-08-26.md` (Gaps: G1, G2, G3 fail-closed routing; F4 workdir circularity; F9 branch cleanup ordering)
- `historical_docs/PLAN-2026-08-26.md`

## Cards in Scope
- **Baseline / Docs / Sweep**: `T01`, `T16`, `T31`
- **State & Metadata Persistence**: `T21`, `T22`, `T45`
- **Checkpoints & Resume**: `T26`, `T54`, `T56`, `T57`, `T58`
- **Enums & Callsite Typing**: `T28`, `T29`, `T30`
- **Fail-Closed & Review Model**: `T43`, `T55`

## Target Production & Test Paths
- `harness/workflow/pipeline.py`
- `harness/workflow/task_lifecycle.py`
- `harness/workflow/resume.py`
- `harness/workflow/continue_fresh.py`
- `harness/core/enums.py`
- `harness/core/config.py`
- `tests/test_resume.py`
- `tests/test_continue_fresh.py`
- `tests/test_workdir_persistence.py` (and related test suites)

## Verification & Audit Items

### 1. Specification Assessor Fail-Closed Routing (Hardening G1, G2, G3, T43)
- Verify `Pipeline.stage_spec()`:
  - Is approval granted **only** on positive approval verdict?
  - Do `kickback`, `error`, `unknown`, `no_verdict`, `fail`, crashes, or unexpected results fail closed (triggering retry/park rather than advancing as approved)?
  - Is there an executable regression test guaranteeing non-approval verdicts cannot advance spec?

### 2. Task State Updates & Workdir Persistence (Audit F4, Hardening F4, T21, T22, T45)
- Verify `task.json` `"status"` field:
  - Is it updated upon `park()`, `fail()`, and `complete()` (not just `intake()`)?
  - Is I/O failure during terminal moves handled safely without corrupting task state?
- Verify workdir flow:
  - Was the circular dependency between intake and `original.md` resolution eliminated?
  - Is `workdir` correctly stored in `task.json` at intake and faithfully reused across resume cycles?

### 3. Stage & Verdict Enum Discipline (T28, T29, T30)
- Verify `Verdict` and `Stage` enum classes in `harness/core/enums.py`:
  - Are raw string literals eliminated from stage dispatch and verdict comparison at callsites?
  - Are enum values correctly converted at boundaries (e.g., stats logging or JSON serialization)?

### 4. Checkpoint State & Crash Resilience (T26, T54, T57, T58)
- Verify per-slice checkpointing:
  - Does atomic checkpoint write ensure no partial slices are marked complete on crash?
  - Does `resume` correctly skip completed slices while preserving prefix validity?
  - Are "all attempts crashed" scenarios recorded with truthful failure reasons?

## Expected Deliverable
Write report to `reviews/REPORT-PHASE-1.md` containing:
- Per-card status table (`[Card] -> [Implemented: YES/NO/PARTIAL] -> [Tests: PASS/FAIL] -> [Findings]`)
- Explicit assessment of Hardening items G1, G2, G3, F4, and Audit F4
- Any deviations, bugs, or residual technical debt identified
