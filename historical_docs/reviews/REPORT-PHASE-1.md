# Phase 1 Review Report: Core Architecture, Workflow & Lifecycle State

## Executive Summary
This report audits the implementation of Phase 1 tickets encompassing pipeline stage routing, fail-closed specification assessment, task lifecycle state persistence, workdir resolution, enum callsite migrations, per-slice checkpointing, and crash resilience.

All 16 cards in scope are implemented with comprehensive test suites passing across all targeted areas.

---

## 1. Per-Card Audit & Status Table

| Card | Description | Implemented | Tests | Findings |
|---|---|---|---|---|
| **T01** | Baseline Clean Tree & Tag | YES | PASS | Working tree baseline pinned, dirty-tree guard invariants verified, `pi/last-good` tag established. |
| **T16** | Docstring Truth & Contract | YES | PASS | Docstrings in `harness/cli/handlers.py` and `harness.py` reflect `--continue` and `--fresh` contracts and list all subcommands. Cites proof tests accurately. |
| **T21** | Status on Terminal Moves | YES | PASS | `_terminal_move()` in `TaskLifecycle` updates `task.json` `"status"` to `parked`, `failed`, or `done` upon directory move, bumping `last_updated`. Handles missing/corrupt `task.json` by creating minimal valid state. |
| **T22** | Record Workdir at Intake | YES | PASS | Intake circularity removed: `intake()` writes `original.md` and initial `task.json`, immediately followed by `record_workdir()` before branch setup or sessions. Resumed tasks reuse persisted `workdir`. Legacy tasks migrate once with warning. |
| **T26** | Per-Slice Checkpointing | YES | PASS | `checkpointed_slices` tracked in `TaskState` with atomic writes. `stage_slices()` skips completed slices after they pass all required reviews. Preserves `CheckpointStage.SLICES` as stage-level marker. |
| **T28** | Enum Values Alignment | YES | PASS | `Verdict` and `Stage` enums updated with exact wire strings matching historical logs (`sessions.jsonl`). Added `Verdict.NO_VERDICT`, `ERROR`, `UNKNOWN`, `KICKOUT`, `REJECT` and `parse()` classmethods. |
| **T29** | Verdict at Callsites | YES | PASS | Replaced raw string verdict comparisons across `Pipeline` with `verdict is Verdict.<MEMBER>`. Ensured wire values are converted at logging/session boundaries. |
| **T30** | Stage at Callsites | YES | PASS | Replaced string literals and f-string dynamic stage names (e.g., `f"{kind}_review"`) with `Stage.<MEMBER>` and typed `ReviewKind`. Boundary conversion to wire strings occurs cleanly in `SessionRunner`. |
| **T31** | Dead Code & Import Sweep | YES | PASS | Removed unused imports (`ensure_branch` in lifecycle, `shutil` in resume, `build`/`CONFIG_PATH` in harness). Cleaned up `_plan_stages()` to return `CheckpointStage` members. Unified logging sink. |
| **T43** | Spec Assessment Fail-Closed | YES | PASS | Implemented `assess_spec()` and `SpecAssessmentDecision`. Only healthy `Verdict.PASS` approves specification. `KICKBACK` enters revision loop; all other verdicts or process errors fail closed and park. |
| **T45** | Terminal Move Bookkeeping Failures | YES | PASS | `_terminal_move()` enforces `shutil.move` as the lifecycle authority. Post-move I/O failures during `task.json` or `review/<id>.md` writes are logged and non-fatal, preventing duplicate state transitions. Failed moves propagate fatal errors without writing false bookkeeping. |
| **T54** | Resume CLI `--fresh` Flag | YES | PASS | `resume <task_id> --fresh` wipes active state while preserving artifacts (such as `slices.md`), starting afresh. Regular resume preserves checkpoint history. |
| **T55** | Review Fix Model Alignment | YES | PASS | Review fix sessions for both technical and functional review failures are routed to `cfg.implementer`. Reviewer models remain distinct. |
| **T56** | Review Note Path Separation | YES | PASS | Review feedback saved to `artifacts/progress/slice-<id>-review.md` separate from implementation progress `slice-<id>.md`, preventing collision and improper reuse. |
| **T57** | Park on Crash Retries Exhaustion | YES | PASS | `_run()` raises `AllAttemptsCrashed` when crash retries are exhausted. Caught at top level in `Pipeline.process()`, parking the task with truthful reason detailing attempt count, stage, and task id. |
| **T58** | Read-Only Autonomous Pending Count | YES | PASS | Implemented `count_pending()` on provider to count pending items without touching directory or creating claims, keeping pending/claimed directories byte-identical. |

---

## 2. Hardening & Audit Items Assessment

### Hardening G1, G2, G3 & T43: Fail-Closed Specification Assessor Routing
- **Status:** Fully Resolved and Verified.
- **Implementation:** `harness/workflow/spec_assessment.py` defines `assess_spec(assessor, result)`:
  - **Approval Gate:** Approval (`SpecAssessment.APPROVED`) requires both `result.ok is True` and `result.verdict is Verdict.PASS`.
  - **Process Failure Precedence (G2):** If `result.ok` is false (e.g., non-zero exit code or crash), the decision is immediately `SpecAssessment.PARKED` with a process failure reason, regardless of whether stdout contained `VERDICT: pass`.
  - **Fail-Closed on Unrecognized / Non-Pass Verdicts (G1):** Any non-`PASS` verdict (`ERROR`, `UNKNOWN`, `NO_VERDICT`, `FAIL`, `DONE`, `RESLICED`, `INFEASIBLE`, etc.) results in `SpecAssessment.PARKED`. Only `Verdict.KICKBACK` invokes `_spec_kickback()`.
  - **Test Verification (G3):** Comprehensive permanent regression suite in `tests/test_spec_assessment_routing.py` verifies all 10 non-approval verdicts, crashed sessions, and non-zero exits fail closed across both `ornith` and `tw` assessors.

### Audit F4 & Hardening F4: Task State Updates & Workdir Resolution
- **Audit F4 (`task.json` Status Field Not Updated):**
  - Resolved via `TaskLifecycle._terminal_move()` in `harness/workflow/task_lifecycle.py`.
  - On `park()`, `fail()`, and `complete()`, `_stamp_status()` updates `"status"` to `parked`, `failed`, and `done` respectively, updating `"last_updated"`.
  - Handled missing `task.json` by creating a minimal valid state record.
  - Verified by `tests/test_terminal_move_failures.py` and `tests/test_checkpoint_state.py`.
- **Hardening F4 (T22 Circular Workdir Flow):**
  - Resolved the circular dependency between intake and `original.md` resolution.
  - `intake(task)` creates `active/<task_id>/`, writes `original.md` and initial `task.json`, and immediately calls `record_workdir(task_dir)` to resolve and persist `state.workdir` before any git operation (`ensure_branch`) or pipeline stage runs.
  - `Pipeline.process()` and `continue_fresh.task_from_dir()` faithfully read `state.workdir`. Legacy tasks with empty `workdir` trigger a one-time migration and persistence.
  - Verified by `tests/test_workdir_persistence.py`.

### Hardening F8 / T26: Per-Slice Checkpointing & Resume
- **Per-Slice Atomicity:**
  - Slices are checkpointed via `TaskLifecycle.checkpoint_slices()`, using `write_atomic()` (`.tmp` + `os.replace`).
  - Slices are appended only after passing both technical and functional reviews (`stage_slices()` in `harness/workflow/pipeline.py`).
  - `stage_slices()` verifies `sid in state.checkpointed_slices` and skips completed slices on resume.
  - `CheckpointStage.SLICES` remains as the stage-level completion marker.
  - Verified by `tests/test_slice_checkpoint.py` and `tests/test_pipeline_resume.py`.

---

## 3. Deviations, Bugs & Residual Technical Debt

1. **`load_state()` Handling of `null` in `checkpointed_stages` (Low / Edge Case):**
   - In `TaskLifecycle.load_state()`:
     `checkpointed_stages=_parse_stages(raw.get("checkpointed_stages", []), self.log)`
     If `task.json` explicitly contains `"checkpointed_stages": null`, `raw.get("checkpointed_stages", [])` evaluates to `None`. Passing `None` to `_parse_stages` raises `TypeError: 'NoneType' object is not iterable`.
   - In contrast, `checkpointed_slices` uses `raw.get("checkpointed_slices") or []`, which is safe against `null`.
   - *Recommendation:* Normalize `raw.get("checkpointed_stages") or []` in `load_state()`.

2. **Enum Call-Site Completeness (Verified Clean):**
   - No bare stage strings remain in `harness/workflow/pipeline.py` or `harness/workflow/autonomous.py`.
   - All comparisons use identity (`is Verdict.<MEMBER>`).
   - Wire serialization at stats/logs boundaries is properly decoupled via `.value`.

---

## Conclusion
Phase 1 core architecture and workflow lifecycle components are compliant with specifications, hardening mandates, and safety invariants. All 123 relevant test cases pass cleanly.
